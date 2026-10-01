import pytest

from universe.eligibility import check_instrument
from universe.exposure_policy import geared_exposure
from universe.models import AssetClass, Instrument


@pytest.mark.parametrize(
    "name",
    [
        "Direxion Daily Small Cap Bear 3X ETF",
        "Direxion Daily 20+ Year Treasury Bear 3X Shares",
        "ProShares UltraShort Silver",
        "ProShares UltraPro Short Dow30",
        "ProShares Ultra VIX Short-Term Futures ETF",
        "ProShares Short S&P500",
        "Tradr 1.5x Long Single Stock ETF",
        "Example -1X ETF",
        "Example Inverse ETF",
        "Example Leveraged ETF",
    ],
)
def test_explicit_geared_objectives_block_even_misclassified_equities(name):
    assert geared_exposure({"asset_name": name})
    instrument = Instrument(
        symbol="TEST", asset_class=AssetClass.STOCK, metadata={"asset_name": name}
    )
    result = check_instrument(instrument)
    assert not result.eligible
    assert "INSTRUMENT_LEVERAGED_OR_INVERSE" in result.reasons


@pytest.mark.parametrize(
    "name",
    [
        "Apple Inc.",
        "Alphabet Inc.",
        "ProShares S&P 500 Dividend Aristocrats ETF",
        "Vanguard Short-Term Bond ETF",
        "iShares Ultra Short-Term Bond ETF",
        "Example 1X Long ETF",
        "Bear Creek Company",
        "",
    ],
)
def test_normal_names_and_short_duration_bonds_are_not_inferred_inverse(name):
    assert not geared_exposure({"asset_name": name})
