"""A MIS on the standard fashion template, for a player without a tuned reader.

The fixtures are the Nykaa Fashion September 2026 MIS and call as supplied. The MIS is in
rupees, uses zero for "not filled in", and lists cancellations and RTO separately while
the call says cancellations already include RTO.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fashion_intel.domain import Provenance
from fashion_intel.engine.mis_adapter import detect_scale, read_generic_mis
from fashion_intel.pipeline import load_player_config, run_month
from fashion_intel.readers.mis import load_mis
from fashion_intel.reference_store import default_reference
from fashion_intel.store import Store

FIX = Path(__file__).parent / "fixtures"
MIS = FIX / "Nykaa Fashion MIS Sep26.xlsx"
CALL = FIX / "Nykaa Fashion Call Sep26.docx"
MONTH = "2026-09"

pytestmark = pytest.mark.skipif(not (MIS.exists() and CALL.exists()), reason="fixtures missing")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    # history_weight=0: the MIS exactly as reported. The history balance is tested separately.
    return run_month("Nykaa Fashion", MONTH, mis_path=str(MIS), transcript_path=str(CALL),
                     out_dir=tmp_path_factory.mktemp("o"), store=Store(tmp_path_factory.mktemp("d")),
                     history_weight=0.0)


@pytest.fixture(scope="module")
def no_call(tmp_path_factory):
    return run_month("Nykaa Fashion", MONTH, mis_path=str(MIS),
                     out_dir=tmp_path_factory.mktemp("o"), store=Store(tmp_path_factory.mktemp("d")),
                     history_weight=0.0)


@pytest.fixture(scope="module")
def cfg():
    return load_player_config("Nykaa Fashion")


@pytest.fixture(scope="module")
def gm(cfg):
    return read_generic_mis(load_mis(str(MIS)), MONTH, cfg, 290.0)


def val(res, par):
    v = res.estimate.value_for_par(res.reference, par)
    assert v is not None and not v.is_null, f"par {par} is unavailable"
    return v


# ------------------------------------------------------------------- units

def test_scale_is_detected_from_history():
    assert detect_scale(2_252_120_679, 290.0)[0] == 1e7          # rupees
    assert detect_scale(225.2, 290.0)[0] == 1.0                  # already crore
    # Lakh and million are not guessed: a wrong file's crore figure / 10 would look plausible.
    assert detect_scale(22_521, 290.0)[0] == 1.0
    assert detect_scale(22_521, 290.0, candidates=(1.0, 10.0, 100.0, 1e7))[0] == 100.0
    assert detect_scale(2_252_120_679, None)[0] == 1e7           # no history: magnitude rule


def test_the_mis_is_read_as_rupees(gm):
    assert gm.scale == 1e7
    assert gm.overall["gross_gmv"] == pytest.approx(225.2120679)


def test_zero_fields_are_not_used_as_values(gm):
    assert gm.overall["shipped_gmv"] is None, "Shipped GMV = 0 means not supplied"
    assert gm.overall["rvp_pct"] is None
    assert "Overall / Shipped GMV" in gm.zero_fields
    labels = {c.label for c in gm.categories if c.gmv_cr}
    assert "Men's Sportswear" not in labels and "Women's Westernwear" in labels


# ---------------------------------------------------------------- platform

def test_the_platform_gmv_is_right_and_matches_the_call(run):
    assert val(run, 241).value == pytest.approx(225.2120679)
    assert val(run, 241).provenance is Provenance.REPORTED
    cor = [c for c in run.corroboration.checks if c.target == "Platform GMV"]
    assert cor and cor[0].verdict == "corroborated"


def test_shipped_and_net_gmv_are_derived_not_zero(run):
    gross, canc = val(run, 241).value, val(run, 242).value
    assert val(run, 243).value == pytest.approx(gross * (1 - canc))
    assert val(run, 243).value > 100
    rto, rvp = val(run, 245).value, val(run, 246).value
    assert val(run, 247).value == pytest.approx(val(run, 243).value * (1 - rto - rvp))


def test_cancellation_follows_the_calls_definition(run):
    """The call says 40% includes RTO, and the MIS lists RTO (20%) separately."""
    assert val(run, 242).value == pytest.approx(0.20)
    assert val(run, 245).value == pytest.approx(0.20)
    assert val(run, 245).provenance is Provenance.REPORTED
    assert any(a.driver == "cancellation definition" for a in run.estimate.adjustments)
    assert any(f.code == "CANCELLATION_DEFINITION" for f in run.all_findings)


def test_without_the_call_the_mis_cancellation_is_used_and_flagged(no_call):
    P = load_player_config("Nykaa Fashion")["platform_rows"]
    assert no_call.estimate.num(P["cancellation_pct"]) == pytest.approx(0.40)
    assert any(f.code == "CANCELLATION_DEFINITION" for f in no_call.all_findings)


def test_aov_and_asp_are_taken_when_they_agree_with_the_model(run):
    assert val(run, 240).value == 3500.0 and val(run, 240).provenance is Provenance.REPORTED
    assert val(run, 468).value == 1500.0 and val(run, 468).provenance is Provenance.REPORTED
    assert val(run, 519).value == pytest.approx(3500.0 / 1500.0)


def test_units_that_do_not_fit_the_model_are_recorded_not_used(run):
    assert any(f.code == "MIS_DEFINITION_CONFLICT" and "Orders" in f.message
               for f in run.all_findings)


# -------------------------------------------------------------- categories

def test_category_gmv_comes_from_the_mis(run, gm):
    shipped = val(run, 243).value
    total = gm.category_total_cr
    by = {c.label: c for c in gm.categories}
    for label, par in (("Women's Westernwear", 1539), ("Women's Ethnicwear", 1538),
                       ("Jewellery", 1552), ("Men's Westernwear", 1544)):
        assert val(run, par).value == pytest.approx(by[label].gmv_cr / total * shipped, rel=1e-9)
        assert val(run, par).provenance is Provenance.DERIVED


def test_category_asp_is_reported(run):
    assert val(run, 1361).value == 1500 and val(run, 1361).provenance is Provenance.REPORTED
    assert val(run, 1362).value == 1200
    assert val(run, 1377).value == 1200


def test_categories_the_mis_left_blank_are_null_not_estimated(run):
    for par in (1546, 1543, 1553):      # men's sportswear, men's ethnic, LTA: all zero in the MIS
        v = run.estimate.value_for_par(run.reference, par)
        assert v is None or v.is_null


def test_the_apparel_tree_adds_up_and_fits_the_platform(run):
    n = lambda p: (val(run, p).value if (run.estimate.value_for_par(run.reference, p)
                                          and not run.estimate.value_for_par(run.reference, p).is_null)
                   else 0.0)
    assert val(run, 554).value == pytest.approx(n(1537) + n(1542) + n(1551), rel=1e-9)
    assert val(run, 457).value == pytest.approx(val(run, 554).value + n(559) + n(562), rel=1e-9)
    assert val(run, 457).value <= val(run, 243).value


def test_home_and_price_buckets_are_filled(run):
    assert val(run, 458).value > 0                           # Home
    assert val(run, 1413).value > 0 and val(run, 1418).value > 0
    # The MIS gives one 500-1000 bucket (20% of GMV); the two output rows must add back to it.
    # The buckets are shares of shipped GMV, so together they add up to it (the engine rescales them).
    assert sum(val(run, p).value for p in range(1413, 1419)) == pytest.approx(val(run, 243).value, rel=1e-6)
    assert val(run, 1415).provenance is Provenance.DERIVED
    assert any(f.code == "MIS_BUCKET_SPLIT" for f in run.all_findings)
    buckets = [val(run, p).value for p in (1413, 1414, 1415, 1416, 1417, 1418)]
    assert sum(buckets) == pytest.approx(val(run, 243).value, rel=1e-6), "buckets must sum to shipped GMV"


def test_category_total_disagreement_is_reported_not_hidden(run):
    f = [x for x in run.all_findings if x.code == "MIS_CATEGORY_RECONCILIATION"]
    assert f and "+21.7%" in f[0].message


def test_the_unit_decision_is_in_the_findings(run):
    f = [x for x in run.all_findings if x.code == "MIS_UNITS"]
    assert f and "rupees" in f[0].message and "225.2" in f[0].message


# --------------------------------------------------------------- output sheet

def test_the_output_sheet_carries_the_numbers(run):
    import openpyxl
    ws = openpyxl.load_workbook(run.outputs["deliverable"])["Myntra-WF"]
    v = {ws.cell(r, 3).value: ws.cell(r, 5).value for r in range(2, ws.max_row + 1)}
    assert v[241] == pytest.approx(225.2121, abs=1e-3)
    assert isinstance(v[243], float) and v[243] > 100
    assert isinstance(v[1539], float) and isinstance(v[554], float) and isinstance(v[457], float)
    assert v[242] == pytest.approx(20.0, abs=1e-6)           # percentages in template units


# ----------------------------------------------------------- wrong-player guard

def test_another_players_mis_is_flagged(tmp_path_factory):
    r = run_month("Clovia", MONTH, mis_path=str(MIS), out_dir=tmp_path_factory.mktemp("o"),
                  store=Store(tmp_path_factory.mktemp("d")))      # Clovia is ~13 Cr; this MIS is 225
    assert any(f.code == "MIS_LEVEL_MISMATCH" for f in r.all_findings)


def test_myntra_given_another_players_mis_is_stopped(tmp_path_factory):
    """Nykaa's rupee MIS read against Myntra's history is ~0.02x: refuse, do not report it."""
    r = run_month("Myntra", MONTH, mis_path=str(MIS), out_dir=tmp_path_factory.mktemp("o"),
                  store=Store(tmp_path_factory.mktemp("d")))
    assert r.estimate is None
    hit = [f for f in r.findings if f.code == "MIS_LEVEL_MISMATCH"]
    assert hit and hit[0].blocking and "another player" in hit[0].detail


def test_a_mis_in_rupees_is_read_correctly_on_myntras_own_path(tmp_path_factory):
    """Myntra's reader gets the same unit detection: a rupee MIS must not be read as crore."""
    import openpyxl
    src = Path(__file__).resolve().parent.parent / "Myntra MIS.xlsx"
    if not src.exists():
        pytest.skip("Myntra MIS not present")
    wb = openpyxl.load_workbook(src)
    ws = wb.active
    for row in ws.iter_rows(min_row=2):
        lab, cell = row[1], row[2]
        if lab.value == "Shipped GMV" or 10 <= cell.row <= 26:       # shipped and category GMV
            if isinstance(cell.value, (int, float)):
                cell.value = cell.value * 1e7                       # crore -> rupees
    rupees = tmp_path_factory.mktemp("m") / "Myntra MIS rupees.xlsx"
    wb.save(rupees)
    r = run_month("Myntra", MONTH, mis_path=str(rupees), out_dir=tmp_path_factory.mktemp("o"),
                  store=Store(tmp_path_factory.mktemp("d")))
    assert r.estimate is not None
    shipped = r.estimate.value_for_par(r.reference, 243)
    assert shipped.value == pytest.approx(3352.4, abs=1.0), shipped.value
    assert any(f.code == "MIS_UNITS" for f in r.all_findings)

