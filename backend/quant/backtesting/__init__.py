"""Backtesting package — Stage 2 harness + Stage 8 desk adapter."""

from quant.backtesting.desk_strategy import DeskConfluenceStrategy
from quant.backtesting.engine import BacktestEngine
from quant.backtesting.strategy import EmaTrendStub

__all__ = ["BacktestEngine", "DeskConfluenceStrategy", "EmaTrendStub"]
