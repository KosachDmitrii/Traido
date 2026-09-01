#!/usr/bin/env python3
"""Adopt a broker-held position that is missing from the Traido ledger."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from broker.factory import create_broker
from core.config import get_settings
from trading.adopt_orphan import adopt_orphan_position
from trading.intents import INTENTS
from trading.ledger import LEDGER


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Adopt a broker orphan into open_positions.")
    parser.add_argument("symbol", help="Ticker symbol, e.g. LLY")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the row that would be written without changing the book.",
    )
    args = parser.parse_args()

    settings = get_settings()
    broker = create_broker(settings)
    result = await adopt_orphan_position(
        symbol=args.symbol,
        broker=broker,
        ledger=LEDGER,
        intents=INTENTS,
        settings=settings,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") in {"adopted", "already_open", "planned"} else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
