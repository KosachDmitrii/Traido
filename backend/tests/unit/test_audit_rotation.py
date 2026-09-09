from types import SimpleNamespace

from agents.scanner.rotation import fair_order, load_history, record_selection


def test_first_pass_preserves_rank_and_rotation_covers_tail():
    ranked = [SimpleNamespace(symbol=str(i)) for i in range(20)]
    history = {}
    assert fair_order(ranked, 4, history) == ranked
    for cycle in range(1, 12):
        selected = fair_order(ranked, 4, history)[:4]
        assert selected[:2] == ranked[:2]
        assert len({item.symbol for item in selected}) == 4
        for item in selected:
            history[item.symbol] = cycle
    assert set(history) == {item.symbol for item in ranked}


def test_rotation_history_is_durable_and_does_not_claim_completion():
    record_selection("deep", ["AAA", "BBB"])
    first = load_history()
    assert set(first["deep"]) == {"AAA", "BBB"}
    assert "deep_completed" not in first
    # No in-process state is required to reconstruct the same history.
    assert load_history() == first
    record_selection("deep_completed", ["AAA"])
    assert set(load_history()["deep_completed"]) == {"AAA"}


def test_rotation_never_introduces_names_or_expands_budget():
    ranked = [SimpleNamespace(symbol="PASS")]
    assert fair_order(ranked, 1, {"REJECTED": 0, "PASS": 10}) == ranked
