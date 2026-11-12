"""Safeguards against an over-quoted GMV when evidence is thin.

Nykaa Fashion, Sep'26: history alone said 290.6 Cr of gross GMV. The call and MIS say ~225 Cr.
The method learned a +21.7% September uplift from three earlier Septembers, but the expert says
September 2026 was an ordinary month. These tests pin down how the system now behaves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fashion_intel.domain import Confidence, Provenance
from fashion_intel.engine import estimators as E
from fashion_intel.pipeline import run_month
from fashion_intel.readers.transcript import load_transcript
from fashion_intel.store import Store

FIX = Path(__file__).parent / "fixtures"
MIS = FIX / "Nykaa Fashion MIS Sep26.xlsx"
CALL = FIX / "Nykaa Fashion Call Sep26.docx"
MONTH = "2026-09"
pytestmark = pytest.mark.skipif(not (MIS.exists() and CALL.exists()), reason="fixtures missing")


def go(tmp, **kw):
    return run_month("Nykaa Fashion", MONTH, out_dir=tmp.mktemp("o"), store=Store(tmp.mktemp("d")), **kw)


def gross(r):
    return r.estimate.value_for_par(r.reference, 241)


@pytest.fixture(scope="module")
def nothing(tmp_path_factory):
    return go(tmp_path_factory)


@pytest.fixture(scope="module")
def call_only(tmp_path_factory):
    return go(tmp_path_factory, transcript_path=str(CALL))


def test_a_call_that_states_the_gmv_replaces_the_history_estimate(call_only):
    g = gross(call_only)
    assert g.provenance is Provenance.CALL_STATED
    assert g.value == pytest.approx(225.0)
    assert any(f.code == "GMV_FROM_CALL" for f in call_only.all_findings)


def test_the_call_saying_both_months_were_bau_suppresses_the_uplift():
    t = load_transcript(str(CALL))
    assert t.event_shift.detected, "'both were BAU months' means no sale lifted the month"


def test_stored_history_that_disagrees_with_the_call_is_flagged(call_only):
    f = [x for x in call_only.all_findings if x.code == "HISTORY_BASE_CONFLICT"]
    assert f and "Sep'25" in f[0].message and "+14%" in f[0].message


def test_history_only_gmv_is_labelled_low_confidence_with_a_range(nothing):
    g = gross(nothing)
    assert g.confidence is Confidence.LOW
    f = [x for x in nothing.all_findings if x.code == "GMV_HISTORY_ONLY"]
    assert f and "range" in f[0].message and "missed by up to" in f[0].detail
    ship = nothing.estimate.value_for_par(nothing.reference, 243)
    assert any(n.startswith("History only") for n in ship.notes)


def test_history_only_is_no_longer_taken_at_full_uplift(nothing):
    assert gross(nothing).value < 285.0, "the 290.6 produced by full September uplift"


def test_a_mis_or_a_call_removes_the_deviation(call_only, tmp_path_factory):
    as_reported = go(tmp_path_factory, mis_path=str(MIS), history_weight=0.0)
    for r in (call_only, as_reported):
        assert abs(gross(r).value / 225.0 - 1) < 0.01


def test_with_the_default_history_balance_the_mis_figure_is_still_kept(tmp_path_factory):
    r = go(tmp_path_factory, mis_path=str(MIS))
    g = gross(r)
    assert g.uncalibrated == pytest.approx(225.2120679)          # the MIS figure, kept beside it
    assert abs(g.value / g.uncalibrated - 1) <= 0.15              # and the move is bounded


# ---- the damping rule itself

def _series(n=60, uplift=0.25, flat_after=None):
    s = {}
    for i in range(n):
        y, m = 2021 + (i + 0) // 12, i % 12 + 1
        base = 100 * (1.01 ** i)
        s[f"{y}-{m:02d}"] = base * (1 + uplift if m == 9 else 1.0)
    return s


def test_strength_is_chosen_from_months_before_the_target_only():
    s = _series()
    a = E.choose_strength(s, "2025-09")[0]
    s2 = dict(s)
    for m in list(s2):
        if m >= "2025-09":
            s2[m] = s2[m] * 5            # wreck everything from the target onward
    b = E.choose_strength(s2, "2025-09")[0]
    assert a == b, "the choice must not see the target month or later"


def test_a_real_seasonal_pattern_keeps_most_of_its_uplift():
    assert E.choose_strength(_series(uplift=0.30), "2026-09")[0] >= 0.75


def test_no_seasonality_in_the_data_gives_no_uplift():
    # Level with noise unrelated to the calendar month: nothing for a seasonal factor to learn.
    flat = {k: 100 + ((i * 37) % 11 - 5) for i, k in enumerate(sorted(_series()))}
    assert E.choose_strength(flat, "2026-09")[0] <= 0.25


def test_too_little_history_falls_back_to_the_default_and_says_so():
    s, table, basis = E.choose_strength({f"2025-{m:02d}": 100.0 for m in range(1, 10)}, "2025-10")
    assert s == E.DEFAULT_STRENGTH and "too few" in basis


def test_damping_pulls_the_estimate_toward_the_flat_level():
    s = _series(uplift=0.30)
    full = E.seasonal_naive(s, "2026-09", seasonal_strength=1.0).value
    half = E.seasonal_naive(s, "2026-09", seasonal_strength=0.5).value
    flat = E.seasonal_naive(s, "2026-09", apply_seasonal=False).value
    assert flat < half < full

