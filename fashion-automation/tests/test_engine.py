"""Unit tests for the calculation engine: weights, estimators, calibration.

These exercise the engine on synthetic data so they do not depend on the
supplied workbooks. `test_myntra_run.py` covers the real files.
"""

from __future__ import annotations

import math

import pytest

from fashion_intel.domain import (Adjustment, Confidence, Provenance, Value,
                                  month_label, null, shift_months)
from fashion_intel.engine import estimators as est
from fashion_intel.engine.calibrate import ImpliedError
from fashion_intel.engine.weights import resolve_weights

WEIGHTS = {
    "weights": {
        "expert_call": {"weight": 0.35, "label": "Expert call"},
        "player_mis": {"weight": 0.35, "label": "MIS"},
        "logistics": {"weight": 0.10, "label": "Logistics"},
        "traffic": {"weight": 0.08, "label": "Traffic"},
        "seasonality": {"weight": 0.05, "label": "Seasonality"},
        "market_events": {"weight": 0.04, "label": "Market events"},
        "social": {"weight": 0.03, "label": "Social"},
    },
    "confidence_thresholds": {"high": 0.75, "medium": 0.50},
}


# ------------------------------------------------------------------- domain

def test_only_reported_is_actual():
    assert Provenance.REPORTED.is_actual
    for p in Provenance:
        if p is not Provenance.REPORTED:
            assert not p.is_actual, f"{p} must not count as a reported actual"


def test_null_is_none_not_zero():
    n = null("INR Cr", "no evidence")
    assert n.value is None and n.is_null
    assert n.provenance is Provenance.UNAVAILABLE
    assert n.value != 0


def test_month_helpers():
    assert month_label("2026-09") == "Sep'26"
    assert shift_months("2026-09", -12) == "2025-09"
    assert shift_months("2026-01", -1) == "2025-12"
    assert shift_months("2026-12", 1) == "2027-01"


def test_value_rejects_non_numeric():
    with pytest.raises(TypeError):
        Value("3352", Provenance.REPORTED)


def test_adjustment_delta():
    a = Adjustment("gmv", "driver", 100.0, 110.0, "why", "src")
    assert a.delta_pct == pytest.approx(0.10)
    assert Adjustment("m", "d", None, 5.0, "", "").delta_pct is None
    assert Adjustment("m", "d", 0.0, 5.0, "", "").delta_pct is None


# ------------------------------------------------------------------ weights

def test_all_sources_present_keeps_configured_weights():
    r = resolve_weights(WEIGHTS, {k: True for k in WEIGHTS["weights"]})
    for k, s in r.sources.items():
        assert s.effective == pytest.approx(WEIGHTS["weights"][k]["weight"])
    assert r.confidence is Confidence.HIGH


def test_missing_sources_redistribute_proportionally():
    avail = {"expert_call": True, "player_mis": True, "seasonality": True,
             "market_events": True, "logistics": False, "traffic": False, "social": False}
    r = resolve_weights(WEIGHTS, avail)
    assert sum(r.effective.values()) == pytest.approx(1.0)
    assert r.effective["logistics"] == 0.0
    # Relative weight between surviving sources is preserved.
    assert (r.effective["expert_call"] / r.effective["seasonality"]
            == pytest.approx(0.35 / 0.05))
    assert r.effective["expert_call"] == pytest.approx(0.35 / 0.79)
    assert r.available_configured_weight == pytest.approx(0.79)


def test_quality_discount_reduces_contribution():
    avail = {k: True for k in WEIGHTS["weights"]}
    r = resolve_weights(WEIGHTS, avail, {"expert_call": [("garbled", 0.6)]})
    assert sum(r.effective.values()) == pytest.approx(1.0)
    assert r.effective["expert_call"] < 0.35
    assert r.effective["player_mis"] > 0.35      # others absorb the released weight
    assert any("discounted" in w for w in r.warnings)


def test_weights_must_sum_to_one():
    bad = {"weights": {"a": {"weight": 0.5}, "b": {"weight": 0.2}}}
    with pytest.raises(ValueError, match="sum to"):
        resolve_weights(bad, {"a": True, "b": True})


def test_no_sources_available_is_reported_not_guessed():
    r = resolve_weights(WEIGHTS, {k: False for k in WEIGHTS["weights"]})
    assert all(v == 0.0 for v in r.effective.values())
    assert r.confidence is Confidence.NONE
    assert any("No evidence source" in w for w in r.warnings)


def test_confidence_bands():
    mk = lambda ks: resolve_weights(WEIGHTS, {k: k in ks for k in WEIGHTS["weights"]})
    assert mk({"expert_call", "player_mis", "logistics"}).confidence is Confidence.HIGH
    assert mk({"expert_call", "player_mis"}).confidence is Confidence.MEDIUM
    assert mk({"expert_call"}).confidence is Confidence.LOW


# --------------------------------------------------------------- estimators

def _flat(start="2023-01", n=40, value=100.0):
    return {shift_months(start, i): value for i in range(n)}


def test_trailing_mean_excludes_target():
    s = {"2026-06": 10.0, "2026-07": 20.0, "2026-08": 30.0, "2026-09": 999.0}
    m, n = est.trailing_mean(s, "2026-09", 3)
    assert m == pytest.approx(20.0) and n == 3


def test_seasonal_naive_flat_series():
    e = est.seasonal_naive(_flat(), "2026-05")
    assert e.ok and e.value == pytest.approx(100.0)


def test_seasonal_factor_detects_a_spike():
    s = _flat(n=48, value=100.0)
    for y in (2023, 2024, 2025):
        s[f"{y}-09"] = 150.0            # every September runs 50% hot
    sf, n = est.seasonal_factor(s, "2026-09")
    assert n == 3
    assert sf == pytest.approx(1.5, rel=0.02)


def test_suppressing_seasonality_holds_the_trailing_level():
    s = _flat(n=48, value=100.0)
    for y in (2023, 2024, 2025):
        s[f"{y}-09"] = 150.0
    applied = est.seasonal_naive(s, "2026-09", apply_seasonal=True)
    held = est.seasonal_naive(s, "2026-09", apply_seasonal=False,
                              seasonal_suppression_reason="event moved")
    assert applied.value > held.value
    assert held.value == pytest.approx(100.0)
    assert "suppressed" in held.method and "event moved" in held.method


def test_estimator_never_sees_the_target_month():
    s = _flat(n=40, value=100.0)
    s["2026-05"] = 10_000.0             # an absurd target-month value
    e = est.seasonal_naive(s, "2026-05")
    assert e.value == pytest.approx(100.0), "target month leaked into its own estimate"


def test_no_history_returns_none_not_zero():
    e = est.seasonal_naive({}, "2026-09")
    assert e.value is None and not e.ok
    assert e.confidence is Confidence.NONE


def test_clipping_bounds_a_factor_outside_the_recent_range():
    """A seasonal factor is clipped to the range actually observed recently."""
    s = _flat(n=48, value=100.0)
    for y in (2023, 2024):            # spikes older than the 12-month clip window
        s[f"{y}-09"] = 10_000.0
    e = est.seasonal_naive(s, "2026-09", clip_tolerance=0.15)
    assert e.clipped
    assert e.value <= 100.0 * 1.15 + 1e-9


def test_no_clipping_when_the_level_is_within_observed_history():
    """The clip is to observed history, so a spike seen last year is allowed."""
    s = _flat(n=48, value=100.0)
    for y in (2023, 2024, 2025):
        s[f"{y}-09"] = 10_000.0
    e = est.seasonal_naive(s, "2026-09", clip_tolerance=0.15)
    assert not e.clipped
    assert e.value == pytest.approx(10_000.0, rel=0.01)


def test_ratio_split_preserves_the_total():
    parts = est.ratio_split(300.0, {"a": 1.0, "b": 2.0, "c": 3.0})
    assert sum(parts.values()) == pytest.approx(300.0)
    assert parts["c"] == pytest.approx(150.0)


def test_ratio_split_rejects_an_empty_basis():
    with pytest.raises(ValueError):
        est.ratio_split(100.0, {"a": 0.0, "b": 0.0})


def test_yoy_and_mom():
    s = {"2025-09": 100.0, "2026-08": 200.0, "2026-09": 110.0}
    assert est.yoy(s, "2026-09") == pytest.approx(0.10)
    assert est.mom(s, "2026-09") == pytest.approx(-0.45)
    assert est.yoy({"2026-09": 1.0}, "2026-09") is None


def test_yoy_handles_zero_denominator():
    assert est.yoy({"2025-09": 0.0, "2026-09": 5.0}, "2026-09") is None


# ---------------------------------------------------------------- backtest

def test_backtest_is_walk_forward_and_measures_real_error():
    s = _flat(n=40, value=100.0)
    r = est.backtest(s, "flat", min_history=15)
    assert r.n > 0
    assert r.mape == pytest.approx(0.0, abs=1e-9)


def test_seasonal_factor_absorbs_constant_growth():
    """On pure geometric growth the year-ago factor cancels the trailing lag."""
    s = {shift_months("2023-01", i): 100.0 * (1.03 ** i) for i in range(40)}
    r = est.backtest(s, "trend", min_history=15)
    assert r.n > 0
    assert r.mape == pytest.approx(0.0, abs=1e-9)
    assert r.direction_accuracy == pytest.approx(1.0)


def test_trailing_mean_alone_lags_a_rising_series():
    """With the seasonal factor suppressed the estimator must understate growth."""
    s = {shift_months("2023-01", i): 100.0 * (1.03 ** i) for i in range(40)}
    r = est.backtest(s, "trend_bau", min_history=15, apply_seasonal=False)
    assert r.n > 0
    assert r.mpe < 0, "a trailing mean cannot keep up with a rising series"
    assert r.mape > 0.01


def test_backtest_without_enough_history_claims_nothing():
    r = est.backtest(_flat(n=5), "short", min_history=15)
    assert r.n == 0 and r.mape is None and r.mpe is None


# -------------------------------------------------------------- calibration

def test_implied_error_direction_and_magnitude():
    # Reported 100 now, expert says -20% YoY, so last year must have been 125.
    i = ImpliedError("Cat", ["x"], 100.0, -0.20, 125.0, 150.0)
    assert i.implied_prior == pytest.approx(125.0)
    assert i.error_pct == pytest.approx(0.20)
    assert i.direction == "overstated"
    assert ImpliedError("C", ["x"], 100.0, -0.2, 125.0, 100.0).direction == "understated"
