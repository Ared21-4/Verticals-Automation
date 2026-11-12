"""The history balance: keep MoM and YoY from being far off each other, using the stored history.

Nykaa Fashion, Sep'26. The MIS reports 225.2 Cr with MoM -1.6% and YoY +34.5%. On the stored
reference that level gives MoM -2.0% but YoY only +18.2%. The output is moved toward the level
whose growth on that history best matches both, so neither is far off, and the gap to the MIS
narrows month by month because history is saved as the MIS reports it.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from fashion_intel.domain import Provenance
from fashion_intel.engine.growth import balanced_level, blend, trusted_weight
from fashion_intel.history import calibration_records, load_overlay
from fashion_intel.pipeline import run_month
from fashion_intel.reference_store import default_reference
from fashion_intel.store import Store
from fashion_intel.trend import balance_view

FIX = Path(__file__).parent / "fixtures"
MIS, CALL = FIX / "Nykaa Fashion MIS Sep26.xlsx", FIX / "Nykaa Fashion Call Sep26.docx"
MONTH = "2026-09"
need = pytest.mark.skipif(default_reference() is None or not MIS.exists(), reason="inputs missing")


# ------------------------------------------------------------------ the maths

def test_the_balanced_level_sits_between_the_two_growth_implied_levels():
    prior, year, mom, yoy = 229.8, 190.5, -0.0156, 0.3447
    lvl = balanced_level(prior, year, mom, yoy)
    lo, hi = sorted([prior * (1 + mom), year * (1 + yoy)])
    assert lo < lvl < hi


def test_it_is_the_least_squares_point_not_an_arbitrary_one():
    prior, year, mom, yoy = 229.8, 190.5, -0.0156, 0.3447
    f = lambda L: (L / year - (1 + yoy)) ** 2 + (L / prior - (1 + mom)) ** 2
    best = balanced_level(prior, year, mom, yoy)
    for d in (-5.0, -0.5, 0.5, 5.0):
        assert f(best) <= f(best + d)


def test_neither_growth_rate_is_left_far_off_the_other():
    prior, year, mom, yoy = 229.8, 190.5, -0.0156, 0.3447
    lvl = balanced_level(prior, year, mom, yoy)
    yoy_dev, mom_dev = abs(lvl / year - 1 - yoy), abs(lvl / prior - 1 - mom)
    at_mis = (abs(225.2 / year - 1 - yoy), abs(225.2 / prior - 1 - mom))
    assert max(yoy_dev, mom_dev) < max(at_mis)             # the worst miss is smaller
    assert abs(yoy_dev - mom_dev) < abs(at_mis[0] - at_mis[1])   # and the two are closer together


def test_a_single_growth_rate_gives_the_level_it_implies():
    assert balanced_level(None, 190.5, None, 0.30) == pytest.approx(190.5 * 1.30)
    assert balanced_level(100.0, None, 0.10, None) == pytest.approx(110.0)
    assert balanced_level(None, None, 0.1, 0.2) is None


def test_blend_weight_zero_leaves_the_level_and_one_goes_all_the_way():
    assert blend(225.0, 244.0, 0.0, 0.15) == (225.0, False)
    assert blend(225.0, 244.0, 1.0, 0.15)[0] == pytest.approx(244.0)
    assert blend(225.0, 244.0, 0.5, 0.15)[0] == pytest.approx(234.5)


def test_the_move_is_capped():
    new, capped = blend(100.0, 160.0, 1.0, 0.15)
    assert capped and new == pytest.approx(115.0)


def test_history_is_trusted_less_when_it_has_sat_far_from_the_mis():
    assert trusted_weight(1.0, [])[0] == 1.0
    near, far = trusted_weight(1.0, [0.02])[0], trusted_weight(1.0, [0.18])[0]
    assert far < near < 1.0
    assert trusted_weight(1.0, [0.9])[0] == 0.25               # never below the floor


# ------------------------------------------------------------------ Nykaa, end to end

def go(tmp, **kw):
    return run_month("Nykaa Fashion", MONTH, mis_path=str(MIS), transcript_path=str(CALL),
                     out_dir=tmp.mktemp("o"), store=Store(tmp.mktemp("d")), **kw)


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    return go(tmp_path_factory)


@need
def test_the_output_is_no_longer_just_the_mis_figure(run):
    g = run.estimate.value_for_par(run.reference, 241)
    assert g.provenance is Provenance.CALIBRATED
    assert g.uncalibrated == pytest.approx(225.2120679)           # the MIS figure, kept beside it
    assert g.value == pytest.approx(244.0, abs=0.2)
    assert 0.05 < g.value / g.uncalibrated - 1 <= 0.15


@need
def test_mom_and_yoy_on_the_stored_history_are_no_longer_far_off(run):
    rc = run.estimate.reconciliations[241]
    # At the MIS level: YoY 16 points short, MoM spot on. After the balance both are within 8.
    assert abs(rc.yoy_final - rc.yoy_stated) < 0.08
    assert abs(rc.mom_final - rc.mom_stated) < 0.09
    assert abs(rc.yoy_stored - rc.yoy_stated) > 0.15


@need
def test_everything_downstream_follows_the_balanced_level(run):
    e, P = run.estimate, run.config["platform_rows"]
    g, c = e.num(P["gross_gmv"]), e.num(P["cancellation_pct"])
    assert e.num(P["shipped_gmv"]) == pytest.approx(g * (1 - c))
    assert e.num(P["net_gmv"]) == pytest.approx(
        e.num(P["shipped_gmv"]) * (1 - e.num(P["rto_pct"]) - e.num(P["rvp_pct"])))
    fashion, apparel = (run.estimate.value_for_par(run.reference, p).value for p in (457, 554))
    assert fashion <= e.num(P["shipped_gmv"]) and apparel <= fashion


@need
def test_weight_zero_is_the_mis_unchanged_and_half_is_in_between(tmp_path_factory):
    zero, half, full = (go(tmp_path_factory, history_weight=w) for w in (0.0, 0.5, 1.0))
    g = lambda r: r.estimate.value_for_par(r.reference, 241).value
    assert g(zero) == pytest.approx(225.2120679)
    assert g(zero) < g(half) < g(full)
    assert g(half) == pytest.approx((g(zero) + g(full)) / 2, rel=0.01)
    assert zero.estimate.value_for_par(zero.reference, 241).provenance is Provenance.REPORTED


@need
def test_the_deviation_from_the_mis_is_shown_for_every_headline_metric(run):
    bv = balance_view(run.estimate, run.mis_only, run.reference, [], MONTH)
    by = {d["metric"]: d for d in bv["deviations"]}
    assert {"Gross GMV", "Shipped GMV", "Net GMV"} <= set(by)
    assert by["Gross GMV"]["deviation"] == pytest.approx(244.0 / 225.2120679 - 1, abs=0.002)
    assert all(abs(d["deviation"]) < 0.2 for d in bv["deviations"])


@need
def test_the_audit_workbook_has_a_balance_sheet(run):
    ws = openpyxl.load_workbook(run.outputs["audit"])["Balance"]
    labels = {ws.cell(r, 1).value: ws.cell(r, 2).value for r in range(1, ws.max_row + 1)}
    assert labels["Gross GMV reported by the MIS"] == pytest.approx(225.2120679)
    assert labels["Gross GMV in the output"] == pytest.approx(244.0, abs=0.2)
    assert any(f.code == "HISTORY_BALANCE" for f in run.all_findings)


@need
def test_the_output_sheet_carries_the_balanced_figure(run):
    ws = openpyxl.load_workbook(run.outputs["deliverable"])["Myntra-WF"]
    v = {ws.cell(r, 3).value: ws.cell(r, 5).value for r in range(2, ws.max_row + 1)}
    assert v[241] == pytest.approx(244.0, abs=0.2)


@need
def test_the_call_is_still_compared_with_the_mis_figure_not_the_adjusted_one(run):
    c = [x for x in run.corroboration.checks if x.target == "Platform GMV"]
    assert c and c[0].verdict == "corroborated"


# ------------------------------------------------- gap narrows over saved months

def _clean_mis(tmp, month_header="Sep'26", gross_mult=1.0, mom=None, yoy=None):
    """The Nykaa MIS with its geographic split fixed to sum to 1 (runs with errors are not saved)."""
    wb = openpyxl.load_workbook(MIS)
    ws = wb.active
    ws["C1"] = month_header
    for r in range(1, ws.max_row + 1):
        lab = ws.cell(r, 2).value
        if lab == "Tier 2+" and ws.cell(r, 1).value in (None, "Geographic Split"):
            ws.cell(r, 3).value = 0.10
        if r == 2 and gross_mult != 1.0:                        # the Gross GMV row
            ws.cell(r, 3).value = ws.cell(r, 3).value * gross_mult
        if r == 2 and mom is not None:
            ws.cell(r, 4).value, ws.cell(r, 5).value = mom, yoy
    out = tmp / f"MIS {month_header.replace(chr(39), '')}.xlsx"
    wb.save(out)
    return out


@need
def test_history_is_saved_as_the_mis_reports_not_as_the_balanced_output(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("d"))
    mis = _clean_mis(tmp_path_factory.mktemp("m"))
    r = run_month("Nykaa Fashion", MONTH, mis_path=str(mis), transcript_path=str(CALL),
                  out_dir=tmp_path_factory.mktemp("o"), store=store, save_history=True)
    assert r.ok and "history_saved" in r.manifest
    block = load_overlay(store.root, "nykaa_fashion")["months"][MONTH]
    row = str(r.config["platform_rows"]["gross_gmv"])
    assert block["rows"][row]["v"] == pytest.approx(225.2120679), \
        "saved history must be what the player reported"
    cal = block["calibration"]
    assert cal["mis_level"] == pytest.approx(225.2120679)
    assert cal["output_level"] == pytest.approx(244.0, abs=0.2)
    assert cal["history_gap"] > 0.05


@need
def test_the_next_month_trusts_history_less_after_it_sat_far_from_the_mis(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("d"))
    out = tmp_path_factory.mktemp("o")
    mis = _clean_mis(tmp_path_factory.mktemp("m"))
    run_month("Nykaa Fashion", MONTH, mis_path=str(mis), transcript_path=str(CALL),
              out_dir=out, store=store, save_history=True)
    assert [c["month"] for c in calibration_records(store.root, "nykaa_fashion", "2026-10")] == [MONTH]

    mis_oct = _clean_mis(tmp_path_factory.mktemp("m"), "Oct'26", gross_mult=1.03, mom=0.03, yoy=0.40)
    nxt = run_month("Nykaa Fashion", "2026-10", mis_path=str(mis_oct), out_dir=out, store=store)
    rc = nxt.estimate.reconciliations[241]
    assert rc.blend_weight is not None and rc.blend_weight < 1.0
    assert "saved month" in rc.weight_basis
    # The prior month is now the MIS's own 225.2, not a balanced 244, so the MoM base is sound.
    P = nxt.config["platform_rows"]
    assert nxt.reference.value(P["gross_gmv"], MONTH) == pytest.approx(225.2120679)
    bv = balance_view(nxt.estimate, nxt.mis_only, nxt.reference,
                      calibration_records(store.root, "nykaa_fashion", "2026-10"), "2026-10")
    assert bv["earlier"] and bv["earlier"][0]["deviation"] == pytest.approx(0.083, abs=0.01)
