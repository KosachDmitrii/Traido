"""
Tradable universe and sector metadata.

The scanner needs a list of symbols; the Risk Engine needs to know which of
them are the same bet in different clothing. Both come from
`configs/universe.json` so there is exactly one place to change what the desk
is allowed to look at.

Selection resolves in this order, first match wins:

1. `universe` — an explicit list in the watchlist config.
2. `universe_preset` — a named preset from `configs/universe.json`.
3. `universe_groups` — one or more sector groups, optionally plus ETFs.
4. Everything: all groups plus all ETFs.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

UNIVERSE_PATH = Path(__file__).resolve().parent.parent / "configs" / "universe.json"

ETF_SECTOR = "etf"
UNKNOWN_SECTOR = "unknown"


@dataclass(frozen=True)
class Universe:
    symbols: list[str]
    sectors: dict[str, str] = field(default_factory=dict)
    benchmark: str = "SPY"
    sector_etf: dict[str, str] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.symbols)

    def __contains__(self, symbol: object) -> bool:
        return isinstance(symbol, str) and symbol.upper() in set(self.symbols)

    def sector_of(self, symbol: str) -> str:
        return self.sectors.get(symbol.upper(), UNKNOWN_SECTOR)

    def by_sector(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for sym in self.symbols:
            out.setdefault(self.sector_of(sym), []).append(sym)
        return out

    def peers_of(self, symbol: str) -> list[str]:
        """Other symbols in the same sector — the most likely correlation cluster."""
        sector = self.sector_of(symbol)
        if sector in (UNKNOWN_SECTOR, ETF_SECTOR):
            return []
        return [s for s in self.symbols if s != symbol.upper() and self.sector_of(s) == sector]


def load_universe_config(path: Path | None = None) -> dict[str, Any]:
    target = path or UNIVERSE_PATH
    if not target.exists():
        return {}
    with target.open(encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


def _dedupe(symbols: Iterable[str]) -> list[str]:
    """Uppercase, drop blanks and duplicates, keep first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in symbols:
        sym = str(raw).strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def build_universe(
    *,
    explicit: Sequence[str] | None = None,
    preset: str | None = None,
    groups: Sequence[str] | None = None,
    include_etfs: bool | Sequence[str] = False,
    config: dict[str, Any] | None = None,
    path: Path | None = None,
) -> Universe:
    cfg = config if config is not None else load_universe_config(path)
    all_groups: dict[str, list[str]] = cfg.get("groups", {})
    etf_buckets: dict[str, list[str]] = cfg.get("etfs", {})
    presets: dict[str, list[str]] = cfg.get("presets", {})

    sectors: dict[str, str] = {}
    for sector, members in all_groups.items():
        for sym in members:
            sectors[str(sym).upper()] = sector
    for etf_symbols in etf_buckets.values():
        for sym in etf_symbols:
            sectors.setdefault(str(sym).upper(), ETF_SECTOR)

    chosen: list[str]
    wanted_etfs = include_etfs
    if explicit:
        chosen = list(explicit)
    elif preset:
        if preset not in presets:
            raise KeyError(f"unknown universe preset: {preset}")
        chosen = list(presets[preset])
    elif groups:
        chosen = []
        for group in groups:
            if group not in all_groups:
                raise KeyError(f"unknown universe group: {group}")
            chosen.extend(all_groups[group])
    else:
        # "Everything" means everything, ETFs included, unless a caller has
        # explicitly narrowed the buckets.
        chosen = [s for members in all_groups.values() for s in members]
        if wanted_etfs is False:
            wanted_etfs = True

    if wanted_etfs:
        buckets: list[str] = (
            list(etf_buckets.keys()) if wanted_etfs is True else [str(b) for b in wanted_etfs]
        )
        for bucket in buckets:
            chosen.extend(etf_buckets.get(bucket, []))

    return Universe(
        symbols=_dedupe(chosen),
        sectors=sectors,
        benchmark=str(cfg.get("benchmark", "SPY")).upper(),
        sector_etf={k: str(v).upper() for k, v in cfg.get("sector_etf", {}).items()},
    )


def universe_from_watchlist(watchlist: dict[str, Any]) -> Universe:
    """
    Resolve the scanner universe from a watchlist config.

    Kept separate from `build_universe` so the watchlist schema can evolve
    without the universe file having to know about it.
    """
    return build_universe(
        explicit=watchlist.get("universe") or None,
        preset=watchlist.get("universe_preset"),
        groups=watchlist.get("universe_groups"),
        include_etfs=watchlist.get("include_etfs", False),
    )


@lru_cache
def default_universe() -> Universe:
    return build_universe()
