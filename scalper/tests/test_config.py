import pytest

from scalper.config import CollectorConfig, load_config


def test_loads_default_config():
    cfg = load_config("config/collector.toml")
    assert cfg.feed == "sip" and "SPY" in cfg.symbols


def test_rejects_bad_feed():
    with pytest.raises(ValueError):
        CollectorConfig(symbols=("SPY",), feed="otc")


def test_rejects_no_streams():
    with pytest.raises(ValueError):
        CollectorConfig(symbols=("SPY",), trades=False, quotes=False)
