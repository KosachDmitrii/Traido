"""ORB ranking is observable volume, independent of invented confidence."""

from types import SimpleNamespace

from agents.scanner.cycle import rank_key


def test_relative_opening_volume_orders_proposals():
    def result(symbol, rv, confidence):
        return SimpleNamespace(
            symbol=symbol,
            candidate=SimpleNamespace(orb_plan={"relative_volume": rv}, confidence=confidence),
        )

    values = [result("AAA", 2, 0.99), result("ZZZ", 5, 0.01)]
    assert [v.symbol for v in sorted(values, key=rank_key)] == ["ZZZ", "AAA"]


def test_symbol_breaks_identical_volume_ties_deterministically():
    values = [
        SimpleNamespace(symbol=s, candidate=SimpleNamespace(orb_plan={"relative_volume": 2}))
        for s in ["ZZZ", "AAA"]
    ]
    assert [v.symbol for v in sorted(values, key=rank_key)] == ["AAA", "ZZZ"]
