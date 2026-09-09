import pytest

from core.enums import SetupType
from core.schemas import AdmissionSnapshot
from tests.unit.test_buy_viability import NOW, _candidate, _quote
from trading.trade_admission import candidate_entry_zone, entry_allowed_for_setup_type
from trading.viability import assess_buy_viability


def candidate():
    snap = AdmissionSnapshot(
        price_at_creation=100, atr_at_creation=1, entry_zone_low=99.8, entry_zone_high=100.1
    )
    return _candidate().model_copy(
        update={
            "setup_type": SetupType.PULLBACK_CONTINUATION,
            "admission_snapshot": snap.model_dump(mode="json"),
        }
    )


@pytest.mark.parametrize("ask", [99.0, 99.6, 99.8, 100.0, 100.1, 100.3, 100.31])
def test_preview_matches_final_zone_predicate(ask):
    cand = candidate()
    quote = _quote(str(ask - 0.01), str(ask))
    expected, reasons = entry_allowed_for_setup_type(
        cand.setup_type, ask, *candidate_entry_zone(cand)
    )
    preview = assess_buy_viability(cand, quote, now=NOW, entry_buffer_bps=0)
    assert preview.buyable == expected
    if not expected:
        assert list(preview.reasons) == reasons
        assert preview.state == "outside_zone"
    assert preview.measured["ask"] == str(quote.ask)
    assert preview.measured["allowed_zone_high"] == pytest.approx(100.3)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"setup_type": SetupType.UNKNOWN}, "SETUP_TYPE_UNKNOWN"),
        ({"admission_snapshot": None}, "MISSING_ENTRY_ZONE"),
        ({"admission_snapshot": {"broken": True}}, "INVALID_ADMISSION_SNAPSHOT"),
    ],
)
def test_missing_or_invalid_evidence_locks_preview(change, reason):
    preview = assess_buy_viability(
        candidate().model_copy(update=change), _quote("99.99", "100"), now=NOW
    )
    assert not preview.buyable
    assert reason in preview.reasons


def test_snapshot_zone_takes_precedence_over_candidate_fields():
    cand = candidate().model_copy(update={"entry_zone_high": 110})
    assert candidate_entry_zone(cand)[1] == 100.1
    assert not assess_buy_viability(cand, _quote("100.30", "100.31"), now=NOW).buyable
