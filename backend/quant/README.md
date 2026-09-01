# Quant package — deterministic indicators & patterns (Stage 1).
# No LLM code belongs here.

## Modules
- `indicators/` — SMA, EMA, RSI, MACD, ATR, Bollinger, VWAP, relative volume
- `candlestick_patterns/` — doji, hammer, shooting star, engulfing, stars
- `chart_patterns/` — double top/bottom, structure
- `support_resistance/` — swing clusters
- `engine.py` — `compute_features`
- `aggregate.py` — 1H → 4H bars
- `backtesting/` — Stage 2 `BacktestEngine` + `EmaTrendStub` (no LLM)
