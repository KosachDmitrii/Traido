"""Supervisor Agent — orchestrates Stage 3 scan; never places orders."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from agents.market.agent import PROMPT_VERSION as MARKET_PV
from agents.market.agent import assess_market
from agents.news.agent import PROMPT_VERSION as NEWS_PV
from agents.news.agent import assess_news
from agents.strategy.agent import STRATEGY_VERSION, propose_with_entry_timing
from agents.technical.agent import PROMPT_VERSION as TECH_PV
from agents.technical.agent import assess_technical
from core.activity import BOARD
from core.config import Settings, get_settings
from core.enums import Timeframe
from core.ports import AuditPort, MarketDataPort
from core.redaction import redact_secrets
from core.schemas import FeatureSnapshot, PipelineResult
from market_data.factory import create_market_data_port
from quant.engine import compute_features
from trading.gates import check_bar_freshness

DEFAULT_TIMEFRAMES = (Timeframe.D1, Timeframe.H1, Timeframe.M15)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Supervisor:
    def __init__(
        self,
        *,
        market_data: MarketDataPort,
        audit: AuditPort,
        settings: Settings | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.market_data = market_data
        self.audit = audit
        self.settings = settings or get_settings()
        self._clock = clock or _utcnow

    async def scan_symbol(
        self,
        symbol: str,
        *,
        timeframes: tuple[Timeframe, ...] = DEFAULT_TIMEFRAMES,
        lookback_days: int = 400,
    ) -> PipelineResult:
        run_id = uuid4()
        symbol = symbol.upper()
        errors: list[str] = []

        BOARD.set_agent("scanner", status="working", detail="Fetching bars", symbol=symbol)
        BOARD.log("scanner", f"Started analysis of {symbol}", symbol=symbol)

        await self.audit.append(
            "ScanJobStarted",
            "supervisor",
            {"symbol": symbol, "timeframes": [t.value for t in timeframes]},
            pipeline_run_id=run_id,
            entity_type="symbol",
            entity_id=symbol,
        )

        try:
            features = await self._load_features(symbol, timeframes, lookback_days)
            await self.audit.append(
                "FeaturesComputed",
                "quant",
                {"symbol": symbol, "timeframes": [t.value for t in features]},
                pipeline_run_id=run_id,
            )
            BOARD.log("scanner", f"Features ready ({len(features)} TF)", symbol=symbol)

            BOARD.set_agent(
                "technical", status="working", detail="Scoring structure", symbol=symbol
            )
            technical = assess_technical(symbol, features)
            BOARD.set_agent(
                "technical",
                status="done",
                detail=technical.trend,
                symbol=symbol,
                score=technical.score,
            )
            BOARD.log(
                "technical", f"Score {technical.score}/100 · {technical.trend}", symbol=symbol
            )
            await self.audit.append(
                "TechnicalAssessmentReady",
                "technical_agent",
                technical.model_dump(mode="json"),
                pipeline_run_id=run_id,
            )

            BOARD.set_agent("news", status="working", detail="Reading headlines", symbol=symbol)
            news = await assess_news(symbol, self.settings.finnhub_api_key)
            BOARD.set_agent(
                "news",
                status="done",
                detail=news.sentiment,
                symbol=symbol,
                score=news.score,
            )
            BOARD.log("news", f"{news.sentiment} · {news.score}/100", symbol=symbol)
            await self.audit.append(
                "NewsAssessmentReady",
                "news_agent",
                news.model_dump(mode="json"),
                pipeline_run_id=run_id,
            )

            BOARD.set_agent("market", status="working", detail="Regime check", symbol=symbol)
            market = await assess_market(self.settings.fred_api_key)
            BOARD.set_agent(
                "market",
                status="done",
                detail=market.risk_posture,
                symbol=symbol,
                score=market.score,
            )
            BOARD.log(
                "market",
                f"{market.regime.value} · {market.risk_posture}",
                symbol=symbol,
            )
            await self.audit.append(
                "MarketAssessmentReady",
                "market_agent",
                market.model_dump(mode="json"),
                pipeline_run_id=run_id,
            )

            BOARD.set_agent("strategy", status="working", detail="Building proposal", symbol=symbol)
            candidate, entry_bundle = propose_with_entry_timing(
                symbol,
                technical,
                news,
                market,
                features,
                pipeline_run_id=run_id,
            )
            if candidate is None:
                BOARD.set_agent(
                    "strategy",
                    status="done",
                    detail="No setup",
                    symbol=symbol,
                    score=0,
                )
                BOARD.log("strategy", "No TradeCandidate — thresholds not met", symbol=symbol)
                await self.audit.append(
                    "TradeCandidateSkipped",
                    "strategy_agent",
                    {"symbol": symbol, "reason": "thresholds_not_met"},
                    pipeline_run_id=run_id,
                )
                BOARD.set_agent("scanner", status="idle", detail="Waiting next symbol")
                return PipelineResult(
                    pipeline_run_id=run_id,
                    symbol=symbol,
                    status="no_candidate",
                    technical=technical,
                    news=news,
                    market=market,
                    candidate=None,
                    entry_decision=entry_bundle,
                    errors=errors,
                    prompt_versions={
                        "technical": TECH_PV,
                        "news": NEWS_PV,
                        "market": MARKET_PV,
                        "strategy": STRATEGY_VERSION,
                    },
                )

            decision_label = candidate.entry_decision.value if candidate.entry_decision else "buy"
            BOARD.set_agent(
                "strategy",
                status="done",
                detail=f"{decision_label} conf {candidate.confidence:.0%}",
                symbol=symbol,
                score=int(candidate.confidence * 100),
            )
            BOARD.log(
                "strategy",
                f"{decision_label} · conf {candidate.confidence:.0%} · "
                f"quality {candidate.entry_quality} · "
                f"@ {candidate.entry} stop {candidate.stop}",
                symbol=symbol,
            )
            await self.audit.append(
                "TradeCandidateProposed",
                "strategy_agent",
                candidate.model_dump(mode="json"),
                pipeline_run_id=run_id,
            )
            return PipelineResult(
                pipeline_run_id=run_id,
                symbol=symbol,
                status="completed",
                technical=technical,
                news=news,
                market=market,
                candidate=candidate,
                entry_decision=entry_bundle,
                errors=errors,
                prompt_versions={
                    "technical": TECH_PV,
                    "news": NEWS_PV,
                    "market": MARKET_PV,
                    "strategy": STRATEGY_VERSION,
                },
            )
        except Exception as exc:  # noqa: BLE001
            # Scrubbed once, here, because all four sinks below are fed from it
            # and a vendor that authenticates by query parameter puts its key in
            # the exception message. See `core.redaction`.
            detail = redact_secrets(str(exc))
            errors.append(detail)
            BOARD.set_agent("scanner", status="error", detail=detail[:80], symbol=symbol)
            BOARD.log("scanner", f"Failed: {detail}", symbol=symbol, level="error")
            await self.audit.append(
                "ScanJobFailed",
                "supervisor",
                {"error": detail},
                pipeline_run_id=run_id,
            )
            return PipelineResult(
                pipeline_run_id=run_id,
                symbol=symbol,
                status="failed",
                errors=errors,
            )

    async def _load_features(
        self,
        symbol: str,
        timeframes: tuple[Timeframe, ...],
        lookback_days: int,
    ) -> dict[Timeframe, FeatureSnapshot]:
        end = self._clock()
        out: dict[Timeframe, FeatureSnapshot] = {}
        for tf in timeframes:
            # D1 needs a long window for EMA200; H1/M15 do not — a year of
            # hourly pages is what turned Stage 3 into a 429 factory.
            window = lookback_days
            if tf is Timeframe.H1:
                window = min(lookback_days, 90)
            elif tf is Timeframe.M15:
                window = min(lookback_days, 30)
            elif tf is Timeframe.M5:
                window = min(lookback_days, 14)
            start = end - timedelta(days=window)
            bars = await self.market_data.get_bars(symbol, tf, start, end)
            if len(bars) < 30:
                continue

            # Every fact a decision consumes has an age limit, and this is where
            # the decision is drawn: the entry, the stop, the target and the ATR
            # behind them all come from these bars. The liquidity gate checks
            # the daily series much later, at the point capital moves, which is
            # why a paging defect could leave the *hourly* series seven weeks
            # behind on 2026-08-31 and produce cards priced 30% away from the
            # market with nothing objecting.
            #
            # Raising rather than skipping the timeframe: dropping one silently
            # changes which timeframe the strategy prices from, and that is the
            # same class of quiet substitution being fixed here.
            fresh = check_bar_freshness(symbol, bars, now=end)
            if not fresh.passed:
                raise ValueError(
                    f"STALE_BARS:{symbol}:{tf.value}:newest={fresh.measured.get('newest_bar')}"
                )

            out[tf] = compute_features(symbol, tf, bars)
        if not out:
            raise ValueError(f"No usable bars for {symbol}")
        return out


def build_supervisor(
    settings: Settings | None = None,
    *,
    market_data: MarketDataPort | None = None,
) -> Supervisor:
    """One supervisor. Pass the cycle's feed in, or a fresh one is built.

    `market_data` exists because this factory was called inside the scan loop,
    once per symbol, and built a new `AlpacaMarketData` every time. The
    `ScanContext` was created precisely so a cycle would share one feed, and the
    hottest path in the cycle was the one bypassing it.
    """
    from core.audit import create_audit

    settings = settings or get_settings()
    return Supervisor(
        market_data=market_data or create_market_data_port(settings),
        audit=create_audit(),
        settings=settings,
    )
