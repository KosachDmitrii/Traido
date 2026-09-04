"""
Assemble the `RiskContext` for a live candidate.

The Risk Engine is deliberately dumb: it enforces limits against facts it is
handed and never fetches anything itself. This module is where those facts are
gathered — open positions, return correlations, sector exposure, the earnings
calendar, and the market regime.

Every lookup degrades to "unknown" on failure rather than to "safe". An
unavailable correlation matrix must not silently become "uncorrelated", so when
data is missing the corresponding check is skipped and the omission is recorded
in `notes` for the audit trail. Earnings, news and sector are the lookups whose
absence is not merely skipped but refused — the engine decides that, and this
module's job is only to report honestly which of the two it is. Sector comes
from `configs/universe.json` first; Finnhub `profile2` fills names outside the
file, and an empty vendor answer is unclassified, never invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from agents.news.agent import assess_news
from core.enums import NewsCheck, SectorCheck, Timeframe
from core.ports import BrokerPort, MarketDataPort
from core.schemas import Bar
from market_data.providers.earnings import get_earnings_calendar
from market_data.providers.sector import get_sector_resolver
from quant.correlation import CorrelationMatrix, correlation_matrix_from_bars
from risk.risk_engine import RiskContext

CORRELATION_LOOKBACK_DAYS = 180
MAX_CORRELATION_SYMBOLS = 12
"""Cap the correlation fetch so a large book cannot stall a scan cycle."""


@dataclass
class ContextBuildResult:
    context: RiskContext
    notes: list[str] = field(default_factory=list)


async def build_risk_context(
    symbol: str,
    *,
    broker: BrokerPort,
    market_data: MarketDataPort | None,
    finnhub_api_key: str | None = None,
    regime_tradable: bool | None = None,
    news: NewsCheck | None = None,
    now: datetime | None = None,
) -> ContextBuildResult:
    """Assemble everything outside the candidate that can veto a trade.

    `news` may be supplied by a caller that has already read the headlines this
    cycle — the scanner has, and re-fetching per symbol would double the vendor
    traffic. Left as `None` it is read here, which is what approval does.
    """
    symbol = symbol.upper()
    now = now or datetime.now(UTC)
    notes: list[str] = []

    open_symbols: list[str] = []
    sector_exposure: dict[str, Decimal] = {}
    unclassified_exposure = Decimal(0)
    sectors = get_sector_resolver(finnhub_api_key)
    positions_trusted = True
    unresolved_intents_trusted = True

    try:
        positions = await broker.list_positions()
    except Exception as exc:  # noqa: BLE001 — report unreadable, never pretend empty
        positions = []
        positions_trusted = False
        notes.append(f"positions unavailable: {type(exc).__name__}")

    for pos in positions:
        pos_symbol = pos.symbol.upper()
        if pos_symbol == symbol:
            continue
        open_symbols.append(pos_symbol)
        pos_sector = await sectors.resolve(pos_symbol, now=now)
        notional = abs(pos.qty * pos.avg_entry)
        # A position we cannot classify is kept out of the per-sector map rather
        # than filed under `"unknown"`. Under that key it read as a sector of
        # its own, so two holdings in genuinely different industries capped each
        # other while neither counted towards the sector it was actually in.
        if pos_sector.available and pos_sector.sector is not None:
            sector_exposure[pos_sector.sector] = (
                sector_exposure.get(pos_sector.sector, Decimal(0)) + notional
            )
        else:
            unclassified_exposure += notional
            notes.append(f"sector unclassified: {pos_symbol}")
            if pos_sector.note:
                notes.append(pos_sector.note)

    correlations = None
    if open_symbols and market_data is None:
        notes.append("correlation skipped: no market-data port")
    elif open_symbols and market_data is not None:
        correlations, corr_note = await _correlations(
            [symbol, *open_symbols[:MAX_CORRELATION_SYMBOLS]],
            market_data=market_data,
            now=now,
        )
        if corr_note:
            notes.append(corr_note)

    calendar = get_earnings_calendar(finnhub_api_key)
    earnings = await calendar.get(symbol, now=now)
    if not earnings.available and earnings.note:
        notes.append(earnings.note)
    # The status travels with the dates, so the engine can tell "no print
    # scheduled" from "no calendar". Both arrive here as two None dates.

    # Re-read at approval rather than carried from the scan, for the same
    # reason the calendar is: a card can wait an hour, and the re-check has to
    # be at least as strong as the one that drew it. A headline that broke in
    # the meantime is exactly what this gate is for.
    if news is None:
        news_assessment = await assess_news(symbol, finnhub_api_key)
        news = news_assessment.status
        if news is not NewsCheck.CHECKED and news_assessment.reasons:
            notes.append(news_assessment.reasons[0])

    unresolved = _unresolved_symbols(notes)
    if any("unresolved intents unavailable" in n for n in notes):
        unresolved_intents_trusted = False
    else:
        unresolved_intents_trusted = True

    # Curated universe.json wins; Finnhub fills names outside the file. Either
    # way `"unknown"` never travels as a sector — that is what let a name skip
    # its real sector's cap.
    candidate = await sectors.resolve(symbol, now=now)
    if candidate.note and candidate.status is not SectorCheck.CHECKED:
        notes.append(candidate.note)

    context = RiskContext(
        open_symbols=open_symbols,
        correlations=correlations,
        sector=candidate.sector if candidate.available else None,
        sector_check=candidate.status,
        sector_exposure=sector_exposure,
        unclassified_exposure=unclassified_exposure,
        earnings=earnings.status,
        next_earnings=earnings.next_date,
        last_earnings=earnings.last_date,
        news=news,
        regime_tradable=regime_tradable,
        unresolved_symbols=unresolved,
        positions_trusted=positions_trusted,
        unresolved_intents_trusted=unresolved_intents_trusted,
        now=now,
    )
    return ContextBuildResult(context=context, notes=notes)


def _unresolved_symbols(notes: list[str]) -> frozenset[str]:
    """Symbols with an unsettled order intent.

    A read failure here degrades to "unknown", not to "safe": if we cannot tell
    which symbols are ambiguous, we say so in the notes rather than reporting an
    empty set as though everything were clean.
    """
    from trading.intents import INTENTS

    try:
        return frozenset(INTENTS.unresolved_symbols())
    except Exception as exc:  # noqa: BLE001
        notes.append(f"unresolved intents unavailable: {type(exc).__name__}")
        return frozenset()


async def _correlations(
    symbols: list[str],
    *,
    market_data: MarketDataPort,
    now: datetime,
) -> tuple[CorrelationMatrix | None, str]:
    start = now - timedelta(days=CORRELATION_LOOKBACK_DAYS)
    bars_by_symbol: dict[str, list[Bar]] = {}
    failures: list[str] = []

    for sym in symbols:
        try:
            bars = await market_data.get_bars(sym, Timeframe.D1, start, now)
        except Exception:  # noqa: BLE001
            failures.append(sym)
            continue
        if len(bars) >= 40:
            bars_by_symbol[sym] = bars

    if len(bars_by_symbol) < 2:
        return None, "correlation skipped: insufficient overlapping history"

    note = f"correlation history missing for {', '.join(failures)}" if failures else ""
    return correlation_matrix_from_bars(bars_by_symbol), note
