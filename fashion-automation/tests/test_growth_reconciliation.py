"""Reconciling a reported level with the MoM and YoY the MIS states beside it.

Nykaa Fashion, Sep'26: the MIS reports 225.2 Cr, MoM -1.6%, YoY +34.5%. On the stored history that
level gives MoM -2.0% (fits) but YoY +18.2% (does not). The stored Sep'25 is the odd one out:
the call independently puts it near 167 Cr. The level must not be forced to the YoY on the stored
base, because that would give 256 Cr and a +11% MoM that the MIS and call both contradict.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fashion_intel.engine.growth import reconcile_growth
from fashion_intel.pipeline import run_month
from fashion_intel.readers.mis import load_mis
from fashion_intel.reference_store import default_reference
from fashion_intel.store import Store
from fashion_intel.trend import trend_rows

FIX = Path(__file__).parent / "fixtures"
MIS, CALL = FIX / "Nykaa Fashion MIS Sep26.xlsx", FIX / "Nykaa Fashion Call Sep26.docx"
MONTH = "2026-09"


# ------------------------------------------------------------------ the logic

def test_growth_that_agrees_with_the_level_changes_nothing():
    r = reconcile_growth("GMV", 100.0, "reported", prior_month=100.0, year_ago=80.0,
                         mom_stated=0.0, yoy_stated=0.25)
    assert r.diagnosis == "consistent"
    assert r.year_ago_reconciled == 80.0 and r.year_ago_basis == "stored history"
    assert r.yoy_reconciled == pytest.approx(0.25)


def test_a_wrong_year_ago_base_is_identified_and_the_level_is_left_alone():
    r = reconcile_growth("GMV", 225.2, "reported", prior_month=229.8, year_ago=190.5,
                         mom_stated=-0.0156, yoy_stated=0.3447)
    assert r.diagnosis == "year_ago_base"
    assert r.level == 225.2 and r.adjusted_level is None
    assert r.yoy_stored == pytest.approx(0.182, abs=0.001)
    assert r.year_ago_reconciled == pytest.approx(225.2 / 1.3447)        # implied by the MIS
    assert r.yoy_reconciled == pytest.approx(0.3447)
    assert r.mom_reconciled is None or r.mom_reconciled == pytest.approx(r.mom_stored)
    assert "inconsistent input" in r.narrative and "break the MoM" in r.narrative


def test_an_independent_base_is_preferred_and_gives_a_yoy_that_is_not_simply_the_stated_one():
    r = reconcile_growth("GMV", 225.2, "reported", 229.8, 190.5, mom_stated=-0.0156,
                         yoy_stated=0.3447, call_year_ago=167.0)
    assert r.year_ago_reconciled == 167.0
    assert "the expert's own figure" in r.year_ago_basis
    assert r.yoy_reconciled == pytest.approx(225.2 / 167.0 - 1)
    assert r.yoy_reconciled != pytest.approx(0.3447, abs=1e-4), "must not be forced to the MIS figure"


def test_forcing_the_level_to_the_stored_base_would_have_broken_the_mom():
    r = reconcile_growth("GMV", 225.2, "reported", 229.8, 190.5, mom_stated=-0.0156,
                         yoy_stated=0.3447)
    assert r.yoy_implied_level / 229.8 - 1 > 0.10        # +11%: the MIS says flat
    assert abs(r.mom_gap) < 0.05                          # while the reported level fits the MoM


def test_a_wrong_prior_month_base_is_identified_the_other_way_round():
    r = reconcile_growth("GMV", 100.0, "reported", prior_month=130.0, year_ago=80.0,
                         mom_stated=-0.05, yoy_stated=0.25)
    assert r.diagnosis == "prior_month_base"
    assert r.prior_month_reconciled == pytest.approx(100.0 / 0.95)
    assert r.mom_reconciled == pytest.approx(-0.05)


def test_a_reported_level_is_never_moved_even_when_both_growths_disagree():
    r = reconcile_growth("GMV", 100.0, "reported", prior_month=130.0, year_ago=70.0,
                         mom_stated=0.0, yoy_stated=0.0, level_is_reported=True)
    assert r.diagnosis == "level" and r.adjusted_level is None
    assert "never moved" in r.narrative


def test_an_unreported_level_is_placed_between_the_evidence_not_at_an_extreme():
    r = reconcile_growth("GMV", 100.0, "estimated", prior_month=130.0, year_ago=70.0,
                         mom_stated=0.0, yoy_stated=0.30, level_is_reported=False)
    assert r.diagnosis == "level"
    lo, hi = sorted([r.mom_implied_level, r.yoy_implied_level, 100.0])[::2]
    assert lo < r.adjusted_level < hi
    assert r.adjusted_level != pytest.approx(r.yoy_implied_level)       # not forced to either


def test_no_stated_growth_means_nothing_to_reconcile():
    r = reconcile_growth("GMV", 100.0, "reported", 90.0, 80.0)
    assert r.diagnosis == "insufficient" and r.adjusted_level is None


def test_a_single_stated_rate_is_still_tested():
    r = reconcile_growth("GMV", 100.0, "reported", prior_month=None, year_ago=60.0, yoy_stated=0.30)
    assert r.diagnosis == "year_ago_base"
    assert r.year_ago_reconciled == pytest.approx(100.0 / 1.30)


# ------------------------------------------------------------------ MIS reading

def test_the_mis_growth_columns_are_read():
    c = load_mis(str(MIS)).get(MONTH, "Gross GMV at SP", "Overall")
    assert c.mom == pytest.approx(-0.0156, abs=1e-3)
    assert c.yoy == pytest.approx(0.3447, abs=1e-3)


# --------------------------------------------------------------- Nykaa end to end

pytestmark_ = pytest.mark.skipif(default_reference() is None or not MIS.exists(),
                                 reason="inputs missing")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    return run_month("Nykaa Fashion", MONTH, mis_path=str(MIS), transcript_path=str(CALL),
                     out_dir=tmp_path_factory.mktemp("o"), store=Store(tmp_path_factory.mktemp("d")),
                     history_weight=0.0)       # the reconciled-base view describes the MIS level


@pytest.mark.skipif(default_reference() is None or not MIS.exists(), reason="inputs missing")
def test_nykaa_gross_gmv_level_is_unchanged_and_yoy_is_placed_in_the_right_spot(run):
    g = run.estimate.reconciliations[241]
    assert g.diagnosis == "year_ago_base"
    assert g.level == pytest.approx(225.2120679)                      # not moved
    assert g.yoy_stored == pytest.approx(0.182, abs=0.002)            # what the stored base gives
    assert g.yoy_reconciled == pytest.approx(0.345, abs=0.01)         # near the MIS's +34.5%
    assert g.yoy_reconciled != pytest.approx(0.3447, abs=1e-4)        # but not forced to it
    assert abs(g.mom_stored - (-0.0156)) < 0.01                       # MoM still sound
    assert any(f.code == "GROWTH_RECONCILIATION" for f in run.all_findings)


@pytest.mark.skipif(default_reference() is None or not MIS.exists(), reason="inputs missing")
def test_the_stored_history_is_not_edited(run):
    P = run.config["platform_rows"]
    assert run.reference.value(P["gross_gmv"], "2025-09") == pytest.approx(190.5, abs=0.1)


@pytest.mark.skipif(default_reference() is None or not MIS.exists(), reason="inputs missing")
def test_the_trend_view_shows_both_the_stored_and_the_reconciled_growth(run):
    t = {x["metric"]: x for x in trend_rows(run.estimate, run.reference, run.config, MONTH)}["Gross GMV"]
    assert t["yoy"] == pytest.approx(0.182, abs=0.002)
    assert t["yoy_reconciled"] == pytest.approx(0.349, abs=0.005)
    assert t["year_ago_reconciled"] == pytest.approx(167.0)
    assert "disagrees with the stated growth" in t["flag"]


@pytest.mark.skipif(default_reference() is None or not MIS.exists(), reason="inputs missing")
def test_the_audit_trend_sheet_carries_the_reconciled_columns(run):
    import openpyxl
    ws = openpyxl.load_workbook(run.outputs["audit"])["Trend"]
    heads = [ws.cell(4, c).value for c in range(1, 17)]
    assert "last year, reconciled" in heads and "vs last year, reconciled" in heads
    row = next(r for r in range(5, ws.max_row + 1) if ws.cell(r, 1).value == "Gross GMV")
    col = heads.index("vs last year, reconciled") + 1
    assert ws.cell(row, col).value == pytest.approx(0.349, abs=0.005)


@pytest.mark.skipif(default_reference() is None, reason="reference missing")
def test_runs_without_a_mis_have_nothing_to_reconcile_and_do_not_fail(tmp_path_factory):
    for name in ("Zara", "Ajio"):
        r = run_month(name, MONTH, out_dir=tmp_path_factory.mktemp("o"),
                      store=Store(tmp_path_factory.mktemp("d")))
        assert r.estimate is not None and r.estimate.reconciliations == {}
