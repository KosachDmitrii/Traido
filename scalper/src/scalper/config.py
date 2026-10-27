"""Collector settings, loaded from TOML. Credentials come from the environment only."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CollectorConfig:
    symbols: tuple[str, ...]
    feed: str = "sip"
    trades: bool = True
    quotes: bool = True
    rth_only: bool = True
    data_dir: Path = Path("data")
    flush_rows: int = 50_000
    flush_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.symbols:
            raise ValueError("symbols must not be empty")
        if self.feed not in ("sip", "iex"):
            raise ValueError(f"feed must be 'sip' or 'iex', got {self.feed!r}")
        if not (self.trades or self.quotes):
            raise ValueError("enable at least one of trades / quotes")
        if self.flush_rows < 1 or self.flush_seconds <= 0:
            raise ValueError("flush_rows and flush_seconds must be positive")


def load_config(path: str | Path) -> CollectorConfig:
    raw = tomllib.loads(Path(path).read_text())
    raw["symbols"] = tuple(s.strip().upper() for s in raw.get("symbols", []))
    if "data_dir" in raw:
        raw["data_dir"] = Path(raw["data_dir"])
    return CollectorConfig(**raw)


def credentials() -> tuple[str, str]:
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise SystemExit("ALPACA_API_KEY / ALPACA_SECRET_KEY are not set (see .env.example)")
    return key, secret
