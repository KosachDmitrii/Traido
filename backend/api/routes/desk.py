"""Desk API — light confirmation surface + broker snapshot + SSE."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.responses import Response

from agents.review.agent import build_review
from agents.scanner.agent import (
    STATUS,
    load_watchlist,
    resolve_universe,
    start_scanner,
    wake_scanner,
)
from api.deps import build_execution_service
from broker.factory import create_broker
from core.activity import BOARD
from core.audit import create_audit
from core.config import get_settings
from core.desk_bus import DESK_BUS
from core.enums import UserDecision
from core.schemas import Position
from market_data.providers.company_name import attach_company_names
from trading.desk_viability import attach_buy_viability
from trading.entry_watches import ENTRY_WATCHES
from trading.exits import EXITS, ExitOpportunity
from trading.ledger import LEDGER
from trading.opportunities import OPPORTUNITIES
from trading.pricing import round_equity_price
from trading.reconcile import reconcile_positions
from trading.reconcile_supervisor import RECONCILE
from trading.session_hours import ET, SessionPhase, session_close, session_phase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["desk"])

_RECONCILE_EVERY_SEC = 30.0
"""How often a dashboard poll may drive a reconciliation pass.

Deliberately unchanged when `_BROKER_TTL_SEC` moves. This one governs an action
— reconciliation installs stops, resizes them and flattens what cannot be
protected — while the TTL below governs a view. Tying the two together would
make "refresh the screen faster" mean "act on the broker more often", which is
not a decision anyone would think they were taking.
"""

_BROKER_TTL_SEC = 5.0
"""How stale the numbers on the screen may be.

Costs roughly three Alpaca requests per miss — account, positions, open orders —
so a five-second floor is about 36 a minute against a 200 limit, with the
adapter's own four-second cache absorbing bursts underneath it.
"""
_broker_cache: dict | None = None
_broker_cache_mono = 0.0


def _market_data_quota_payload() -> dict:
    """Live Alpaca data-key quota — what the scanner is actually pacing against."""
    try:
        from market_data.providers.alpaca import account_quota

        return account_quota().as_dict()
    except Exception:  # noqa: BLE001 — desk poll must never fail on status
        return {}


_STREAM_MAX_SEC = 120.0
"""How long one SSE connection lives before the browser is asked to reconnect.

A graceful shutdown waits for in-flight responses *before* it runs the lifespan
shutdown hook, so nothing inside the app gets to speak first: a stream that
never ends on its own holds the server open for as long as a tab stays open.
Under `--reload` that turns every code change into a hang.

EventSource reconnects by itself, so an expiring stream costs one reconnect and
buys a shutdown bounded by this number.
"""


class DeskResponse(BaseModel):
    """Light desk — no live Alpaca calls."""

    mode: str = "confirmation"
    scanner: dict
    buy_opportunities: list
    sell_opportunities: list
    positions: list  # ledger
    review: dict
    activity: dict
    message: str
    rev: int = 0


class BrokerSnapshot(BaseModel):
    portfolio: dict | None = None
    positions: list = Field(default_factory=list)
    open_orders: list = Field(default_factory=list)
    reconciliation: dict = Field(default_factory=dict)
    rev: int = 0
    cached: bool = False
    as_of: float | None = None
    ttl_seconds: float | None = None


class ExitDecisionBody(BaseModel):
    decision: UserDecision = Field(description="sell or hold")


def _light_payload(*, buy_opportunities: list | None = None) -> dict:
    settings = get_settings()
    buys = (
        buy_opportunities
        if buy_opportunities is not None
        else [b.model_dump(mode="json") for b in OPPORTUNITIES.list_open()]
    )
    sells = EXITS.list_open()
    ledger = LEDGER.get_open()
    # Snapshot-only — do not write activity logs on every poll
    review = build_review(live_only=True, announce=False)
    positions_out = [
        {
            "symbol": r.symbol,
            "qty": str(r.qty),
            "avg_entry": str(r.avg_entry),
            "stop": _tick(r.stop_price),
            "target": _tick(r.target_price),
            "strategy_version": r.strategy_version,
        }
        for r in ledger
    ]
    return {
        "mode": settings.trading_mode.value,
        "scanner": {
            "enabled": STATUS.enabled,
            "running": STATUS.running,
            "cycle": STATUS.cycle,
            "last_symbol": STATUS.last_symbol,
            "last_finished_at": STATUS.last_finished_at,
            "symbols_scanned": STATUS.symbols_scanned,
            "opportunities_found": STATUS.opportunities_found,
            "universe": STATUS.universe or resolve_universe(load_watchlist()),
            "error": STATUS.error,
            "funnel": STATUS.funnel.as_dict(),
            # The funnel says how many names survived each stage; these say what
            # it cost and when the next one is due. Together they are the answer
            # to the operator's actual question, which is never "how many were
            # rejected" but "why are there only two cards".
            "stage_seconds": STATUS.stage_seconds,
            "schedule": STATUS.schedule,
            "shortlist": STATUS.shortlist,
            "ai_budget": STATUS.ai_budget,
            "provider_stats": STATUS.provider_stats,
            "market_data_quota": _market_data_quota_payload(),
        },
        "buy_opportunities": buys,
        "entry_watches": [w.model_dump(mode="json") for w in ENTRY_WATCHES.list_open()],
        "sell_opportunities": [s.model_dump(mode="json") for s in sells],
        "positions": positions_out,
        "review": review.to_dict(),
        "activity": BOARD.snapshot(),
        "session": _session_payload(),
        "message": "Agents scan the watchlist. You only confirm BUY or SELL.",
        "rev": DESK_BUS.desk_rev,
    }


def _tick(price: Decimal | None) -> str | None:
    """A price as the venue holds it, for the operator to compare against.

    The ledger keeps the strategy's unrounded geometry — KO's stop is 88.4596 —
    but the order resting at the broker is at 88.46, because that is the tick.
    The desk showed the first next to a broker order list showing the second,
    which invites the reader to conclude they are two different orders.
    """
    return None if price is None else str(round_equity_price(price))


def _mark_to_market(p: Position) -> dict:
    """What the position is worth right now, for the operator to read.

    Absent when the broker did not report a mark. Deliberately absent rather
    than zero: a card showing 0.0% is a card claiming the position is flat, and
    "we do not currently know" is a different thing the operator needs to be
    able to see.

    Display only, on the venue's own valuation. Nothing here is a market data
    read and nothing here may gate a decision — the number carries no age and no
    source, which is exactly what every price-consuming gate requires.
    """
    if p.mark is None or p.avg_entry <= 0:
        return {"mark": None, "pnl": None, "pnl_pct": None}
    return {
        "mark": str(p.mark),
        "pnl": str((p.mark - p.avg_entry) * p.qty),
        "pnl_pct": round(float((p.mark - p.avg_entry) / p.avg_entry * 100), 2),
    }


def _session_payload() -> dict:
    """What the RTH gate will say, and the clock it says it against.

    The desk refuses new entries outside the regular session, so an operator
    looking at a full queue of proposals needs to know that before clicking, not
    after the rejection. The clock is read here rather than in the browser for
    the same reason the phase is: two clocks disagreeing would make the label
    unfalsifiable, and it is the server's clock that decides.
    """
    now = datetime.now(ET)
    phase = session_phase(now)
    return {
        "phase": phase.value,
        # Named for the consequence, not the calendar: protective exits and
        # reconciliation run in every phase.
        "entries_allowed": phase is SessionPhase.REGULAR,
        "et_time": now.strftime("%H:%M"),
        # The exchange's date, next to the exchange's clock. An operator abroad
        # reads the big number against their own wall clock unless something
        # says otherwise, and from 19:00 in Moscow that is yesterday in New
        # York — the same off-by-one day the market_date rule exists to prevent,
        # arriving through the screen instead of through the code.
        "et_date": f"{now:%a} {now:%b} {now.day}",
        "opens_at": "09:30",
        "closes_at": session_close(now.date()).strftime("%H:%M"),
    }


def _etag_for(payload: dict) -> str:
    # Include agent live status so Working/Idle ticks reach the UI (not only card changes).
    agents = (payload.get("activity") or {}).get("agents") or []
    finger = {
        "rev": payload.get("rev"),
        "cycle": (payload.get("scanner") or {}).get("cycle"),
        "running": (payload.get("scanner") or {}).get("running"),
        "last_symbol": (payload.get("scanner") or {}).get("last_symbol"),
        "buys": [
            (
                b.get("id"),
                b.get("status"),
                (b.get("candidate") or {}).get("symbol"),
                # Viability must bust the ETag: a card that goes from live to
                # drifted is the whole reason this reading exists, and a 304
                # would leave BUY enabled on a card the book has already left.
                (b.get("viability") or {}).get("state"),
                (b.get("viability") or {}).get("buyable"),
            )
            for b in payload.get("buy_opportunities") or []
        ],
        "sells": [
            (
                s.get("id"),
                s.get("status"),
                (s.get("proposal") or {}).get("symbol"),
                round(float((s.get("proposal") or {}).get("pnl_pct") or 0), 2),
            )
            for s in payload.get("sell_opportunities") or []
        ],
        "pos": [(p.get("symbol"), p.get("qty")) for p in payload.get("positions") or []],
        "trades": (payload.get("review") or {}).get("trade_count"),
        # Includes the clock, so this busts once a minute on an otherwise idle
        # desk. That is the price of a header clock that does not freeze behind
        # a 304, and one extra payload per minute is not a cost worth avoiding.
        "session": payload.get("session"),
        "agents": [
            # `active` belongs here too: it decays on its own, with no other field
            # changing. Leave it out and the last poll of a pass matches the
            # previous ETag, so the desk keeps animating agents that have stopped.
            (
                a.get("id"),
                a.get("status"),
                a.get("active"),
                a.get("detail"),
                a.get("score"),
                a.get("last_symbol"),
            )
            for a in agents
        ],
    }
    digest = hashlib.sha256(json.dumps(finger, sort_keys=True, default=str).encode()).hexdigest()[
        :16
    ]
    return f'W/"{digest}"'


@router.get("/desk")
async def desk(
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
):
    buys = await attach_buy_viability(OPPORTUNITIES.list_open())
    payload = _light_payload(buy_opportunities=buys)
    key = get_settings().finnhub_api_key
    await attach_company_names(payload["positions"], key)
    await attach_company_names(payload.get("review", {}).get("recent") or [], key)
    etag = _etag_for(payload)
    if if_none_match and if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    return JSONResponse(content=payload, headers={"ETag": etag, "Cache-Control": "no-cache"})


async def _build_broker_snapshot(*, force: bool) -> dict:
    global _broker_cache, _broker_cache_mono

    now = time.monotonic()
    if not force and _broker_cache is not None and (now - _broker_cache_mono) < _BROKER_TTL_SEC:
        out = dict(_broker_cache)
        out["cached"] = True
        return out

    settings = get_settings()
    broker = create_broker(settings)
    audit = create_audit()

    # Reconciliation is the one part of this handler that changes broker state:
    # it installs missing stops, resizes them, flattens what cannot be protected
    # and cancels orphaned entries. It is therefore not run here directly. The
    # supervisor owns it, so that two callers arriving together share one pass
    # instead of both replacing the same missing stop (P0-4, and the delivery
    # mechanism for P0-1).
    #
    # A vendor hiccup must degrade the view rather than fail the request, and
    # the supervisor records the failure instead of raising it — the desk then
    # renders "could not check" rather than a stale number presented as truth.
    await RECONCILE.run_if_stale(
        lambda: reconcile_positions(
            broker,
            audit,
            execution=build_execution_service(broker=broker, audit=audit),
        ),
        max_age_sec=0.0 if force else _RECONCILE_EVERY_SEC,
    )

    # The exit assessment used to run here. It is a control loop — it raises
    # sell proposals and withdraws them again — and a control loop driven by a
    # page render only acts while someone is looking. `agents.position.loop`
    # owns it now; this route reads the result.

    ledger = LEDGER.get_open()
    by_sym = {r.symbol.upper(): r for r in ledger}

    portfolio_dict = None
    try:
        portfolio = await broker.get_portfolio()
        portfolio_dict = {
            "equity": str(portfolio.equity),
            "cash": str(portfolio.cash),
            "buying_power": str(portfolio.buying_power),
            "day_pnl": str(portfolio.day_pnl),
            "open_positions": portfolio.open_positions,
        }
    except Exception:  # noqa: BLE001
        portfolio_dict = (_broker_cache or {}).get("portfolio")

    positions_out = []
    try:
        for p in await broker.list_positions():
            meta = by_sym.get(p.symbol.upper())
            positions_out.append(
                {
                    "symbol": p.symbol,
                    "qty": str(p.qty),
                    "avg_entry": str(p.avg_entry),
                    "stop": _tick(meta.stop_price) if meta else None,
                    "target": _tick(meta.target_price) if meta else None,
                    "strategy_version": meta.strategy_version if meta else None,
                    **_mark_to_market(p),
                }
            )
    except Exception:  # noqa: BLE001
        positions_out = (_broker_cache or {}).get("positions") or [
            {
                "symbol": r.symbol,
                "qty": str(r.qty),
                "avg_entry": str(r.avg_entry),
                "stop": _tick(r.stop_price),
                "target": _tick(r.target_price),
                "strategy_version": r.strategy_version,
            }
            for r in ledger
        ]

    open_orders_out = []
    try:
        for o in await broker.list_open_orders():
            open_orders_out.append(
                {
                    "broker_order_id": o.broker_order_id,
                    "client_order_id": o.client_order_id,
                    "symbol": o.symbol,
                    "side": o.side.value,
                    "order_type": o.order_type.value,
                    "qty": str(o.qty),
                    "filled_qty": str(o.filled_qty) if o.filled_qty is not None else "0",
                    "status": o.status.value,
                    "limit_price": str(o.limit_price) if o.limit_price is not None else None,
                    "stop_price": str(o.stop_price) if o.stop_price is not None else None,
                }
            )
    except Exception:  # noqa: BLE001
        open_orders_out = (_broker_cache or {}).get("open_orders") or []

    if portfolio_dict is not None:
        portfolio_dict = {**portfolio_dict, "open_orders": len(open_orders_out)}

    await attach_company_names(positions_out, get_settings().finnhub_api_key)

    snap = {
        "portfolio": portfolio_dict,
        "positions": positions_out,
        "open_orders": open_orders_out,
        "reconciliation": RECONCILE.status.as_dict(),
        "rev": DESK_BUS.broker_rev,
        "cached": False,
        "as_of": time.time(),
        # Sent so the panel can state its own refresh rate instead of carrying a
        # hardcoded copy of it. The subtitle read "(15–20s)" and would have gone
        # on reading that after this number changed.
        "ttl_seconds": _BROKER_TTL_SEC,
    }
    _broker_cache = snap
    _broker_cache_mono = now
    return snap


@router.get("/desk/broker", response_model=BrokerSnapshot)
async def desk_broker(fresh: bool = Query(default=False)) -> BrokerSnapshot:
    snap = await _build_broker_snapshot(force=fresh)
    return BrokerSnapshot.model_validate(snap)


@router.get("/desk/stream")
async def desk_stream(request: Request):
    """SSE: desk/broker revision hints so UI can refresh without blind polling."""

    async def gen():
        q = DESK_BUS.subscribe()
        deadline = time.monotonic() + _STREAM_MAX_SEC
        try:
            hello = {
                "type": "hello",
                "channel": "desk",
                "desk_rev": DESK_BUS.desk_rev,
                "broker_rev": DESK_BUS.broker_rev,
            }
            # Reconnect promptly: this stream expires on purpose, and the
            # browser's default retry would leave the desk blind meanwhile.
            yield "retry: 1000\n\n"
            yield f"data: {json.dumps(hello)}\n\n"
            while True:
                left = deadline - time.monotonic()
                if left <= 0 or DESK_BUS.closing or await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=min(15.0, left))
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event.get("type") == "closing":
                    break
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            DESK_BUS.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/scanner/run")
async def scanner_run_now() -> dict:
    """Ask for a pass now; the scanner loop performs it.

    This used to run a cycle inline, which meant every caller became a second
    walker over the same universe, writing into the same `STATUS.funnel` and
    doubling the market-data request rate. Waking the one loop that already
    exists gives the caller what it wanted — a pass, soon — without ever
    creating a second one.
    """
    start_scanner()
    wake_scanner()
    return {
        "requested": True,
        "cycle": STATUS.cycle,
        "running": STATUS.running,
        "symbols_scanned": STATUS.symbols_scanned,
        "opportunities_found": STATUS.opportunities_found,
        "error": STATUS.error,
        "open_buys": len(OPPORTUNITIES.list_open()),
    }


@router.post("/positions/{symbol}/close")
async def close_position(symbol: str) -> ExitOpportunity:
    """Flatten a position the operator wants out of, card or no card."""
    service = build_execution_service()
    try:
        result = await service.close_position(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        DESK_BUS.bump_desk(kind="close_failed", symbol=symbol.upper())
        DESK_BUS.bump_broker(kind="close_failed")
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    DESK_BUS.bump_desk(kind="position_closed", symbol=symbol.upper(), status=result.status)
    DESK_BUS.bump_broker(kind="position_closed")
    return result


@router.post("/exits/{exit_id}/decide")
async def decide_exit(exit_id: UUID, body: ExitDecisionBody) -> ExitOpportunity:
    if body.decision not in {UserDecision.SELL, UserDecision.HOLD}:
        raise HTTPException(status_code=400, detail="decision must be sell or hold")
    service = build_execution_service()
    try:
        result = await service.decide_exit(exit_id, body.decision)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        DESK_BUS.bump_desk(kind="exit_failed", exit_id=str(exit_id))
        DESK_BUS.bump_broker(kind="exit_failed")
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    DESK_BUS.bump_desk(kind="exit_decided", exit_id=str(exit_id), status=result.status)
    DESK_BUS.bump_broker(kind="exit_decided")
    return result
