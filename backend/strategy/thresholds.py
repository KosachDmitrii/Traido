"""Formal promotion thresholds — config, not intuition."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class PromotionThresholds:
    """Pass bars for each gate. Changing these does not rewrite past evidence."""

    min_backtest_trades: int = 20
    min_backtest_return_pct: float = 0.0
    min_oos_trades: int = 20
    min_oos_return_pct: float = 0.0
    min_profit_factor: float = 1.2
    min_walk_forward_efficiency: float = 0.4
    min_paper_trades: int = 20
    min_paper_expectancy_usd: float = 0.0
    """Strictly greater than this after costs in the paper journal."""

    min_paper_profit_factor: float = 1.0
    min_regimes_with_trades: int = 1
    """Paper/OOS must not be a single-regime anecdote when regime tags exist."""

    def as_dict(self) -> dict:
        return asdict(self)


_DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config" / "promotion_thresholds.json"


def _load_file(path: Path) -> dict:
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError(f"promotion thresholds file must be an object: {path}")
    return raw


@lru_cache
def get_promotion_thresholds() -> PromotionThresholds:
    path = Path(os.getenv("TRAIDO_PROMOTION_THRESHOLDS_PATH") or _DEFAULT_PATH)
    data = _load_file(path)
    known = {f.name for f in PromotionThresholds.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    filtered = {k: v for k, v in data.items() if k in known}
    return PromotionThresholds(**filtered)
