"""End-to-end acceptance tests against the supplied Myntra files.

These are the acceptance criteria from the brief, expressed as assertions.
They skip rather than fail if the source files are not present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fashion_intel.domain import Provenance, Severity
from fashion_intel.pipeline import load_player_config, run_month
from fashion_intel.readers.mis import load_mis, parse_number
from fashion_intel.readers.reference import load_reference
from fashion_intel.readers.transcript import load_transcript
from fashion_intel.store import Override, Store

ROOT = Path(__file__).resolve().parent.parent
REF = ROOT / "reference" / "Fashion Reference.xlsx"
MIS = ROOT / "Myntra MIS.xlsx"
DOC = ROOT / "Myntra_September_Expert_Call_Insights.docx"
MONTH = "2026-09"

pytestmark = pytest.mark.skipif(
    not (REF.exists() and MIS.exists() and DOC.exists()),
    reason="supplied Myntra source files not present")


@pytest.fixture(scope="module")
def cfg():
    return load_player_config("myntra")


@pytest.fixture(scope="module")
def ref(cfg):
    return load_reference(str(REF), cfg)


@pytest.fixture(scope="module")
def mis():
    return load_mis(str(MIS))


@pytest.fixture(scope="module")
def tr():
    return load_transcript(str(DOC))


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    out = tmp_path_factory.mktemp("out")
    store = Store(tmp_path_factory.mktemp("data"))
    return run_month("myntra", MONTH, str(REF), str(MIS), str(DOC),
                     out_dir=out, store=store)


# ----------------------------------------------------------------- readers

def test_parse_number_handles_mis_formats():
    assert parse_number("80 M") == (80.0, "Mn")
    assert parse_number(0.3) == (0.3, "")
    assert parse_number("30%") == (0.30, "%")
    assert parse_number("1,234 Cr") == (1234.0, "INR Cr")
    assert parse_number(None) == (None, "")
    assert parse_number("") == (None, "")
    assert parse_number("n/a") == (None, "")


def test_mis_sections_are_not_conflated(mis):
    """GMV and ASP share category names; they must stay distinct."""
    gmv = mis.section_values(MONTH, "Category GMV")
    asp = mis.section_values(MONTH, "Category ASP")
    assert len(gmv) == 17 and len(asp) == 17
    assert gmv["Jewellery"] != asp["Jewellery"]
    assert sum(gmv.values()) == pytest.approx(3352.4, abs=0.5)
    with pytest.raises(KeyError, match="sections"):
        mis.get(MONTH, "Jewellery")          # ambiguous without a section


def test_mis_blanks_stay_blank(mis):
    assert mis.get(MONTH, "Gross GMV at SP", "Overall").missing
    assert mis.value(MONTH, "Gross GMV at SP", "Overall") is None
    assert mis.get(MONTH, "COD (%)", "Payment Split").missing


def test_reference_month_axis_excludes_analysis_columns(ref):
    assert ref.has_month(MONTH)
    assert ref.months[0] == "2022-01"
    assert ref.months[-1] == MONTH
    # "Sep MoM" / "Sep YoY" headers must not be read as months.
    assert len(ref.months) == 57


def test_reference_history_never_includes_the_target(ref, cfg):
    row = cfg["platform_rows"]["shipped_gmv"]
    hist = ref.history(row, MONTH)
    assert all(m < MONTH for m, _ in hist)


def test_transcript_extracts_category_yoy(tr):
    assert len(tr.category_yoy) == 13
    assert tr.category_yoy["Women's Sports Apparel"] == pytest.approx(0.20)
    assert tr.category_yoy["Sports Footwear"] == pytest.approx(-0.30)
    assert tr.category_yoy["Personal Care"] == pytest.approx(-0.05)


def test_transcript_knows_no_platform_gmv_was_given(tr):
    assert tr.platform_gmv_given is False
    assert any("no platform GMV" in n for n in tr.notes)


def test_transcript_detects_the_event_shift(tr):
    assert tr.event_shift.detected
    assert tr.event_shift.direction == "out_of_month"
    assert tr.event_shift.evidence


def test_transcript_classifies_forward_looking_statements(tr):
    kinds = {i.kind for i in tr.insights}
    assert "forward_looking" in kinds
    assert "opinion" in kinds
    fwd = [i for i in tr.insights if i.kind == "forward_looking"]
    assert any("October" in i.statement or "will" in i.statement for i in fwd)


# ------------------------------------------------------------ acceptance 1-13

def test_1_workflow_executes(run):
    assert run.estimate is not None
    assert run.run_id
    assert all(Path(p).exists() for p in run.outputs.values())


def test_2_multiple_historical_months_processed(run):
    assert len([m for m in run.reference.months if m < MONTH]) >= 50


def test_3_derived_metrics_reconcile(run, cfg):
    P, e = cfg["platform_rows"], run.estimate
    shipped, canc, gross = e.num(P["shipped_gmv"]), e.num(P["cancellation_pct"]), e.num(P["gross_gmv"])
    assert gross * (1 - canc) == pytest.approx(shipped, rel=1e-9)
    assert e.num(P["net_gmv"]) == pytest.approx(shipped * (1 - e.num(P["returns_pct"])), rel=1e-9)
    assert e.num(P["rto_pct"]) + e.num(P["rvp_pct"]) == pytest.approx(e.num(P["returns_pct"]), rel=1e-12)
    assert e.num(P["aov"]) == pytest.approx(e.num(P["asp"]) * e.num(P["basket_size"]), rel=1e-9)


def test_3b_categories_sum_to_the_platform_total(run, cfg):
    e = run.estimate
    l2 = sum(v["gmv"].value for v in e.l2.values() if v.get("gmv") and v["gmv"].value is not None)
    assert l2 == pytest.approx(e.num(cfg["platform_rows"]["shipped_gmv"]), rel=1e-9)
    seg = sum(e.num(s["shipped_gmv"]) for s in cfg["segments"].values())
    assert seg == pytest.approx(l2, rel=1e-9)
    shares = sum(e.num(m["share"]) for m in cfg["l2_categories"].values())
    assert shares == pytest.approx(1.0, rel=1e-9)


def test_4_output_follows_the_reference_format(run, cfg):
    """Same columns, same row count and the same par_id set as the template.

    Row order is re-sequenced for readability, so the sheet loads by par_id
    rather than by position; the par_id set must still match exactly.
    """
    import openpyxl
    wb = openpyxl.load_workbook(run.outputs["deliverable"])
    ws = wb[cfg["reference"]["output_template_sheet"][:31]]
    assert [ws.cell(1, c).value for c in range(1, 6)] == [
        "parent_question", "parameter_name", "par_id", "unit", "Sep'26"]
    ids = [ws.cell(r, 3).value for r in range(2, ws.max_row + 1)]
    assert len(ids) == len(run.reference.template)
    assert sorted(ids) == sorted(t.par_id for t in run.reference.template)
    assert len(set(ids)) == len(ids)
    # Units and names still travel with their par_id.
    by_id = {t.par_id: t for t in run.reference.template}
    for r in range(2, ws.max_row + 1):
        t = by_id[ws.cell(r, 3).value]
        assert ws.cell(r, 2).value == t.parameter_name
        assert (ws.cell(r, 4).value or "") == t.unit


def test_fashion_gmv_tree_is_present_and_reconciles(run, cfg):
    """The apparel GMV tree must be readable from the sheet and must add up."""
    import openpyxl
    ws = openpyxl.load_workbook(run.outputs["deliverable"])[
        cfg["reference"]["output_template_sheet"][:31]]
    vals = {ws.cell(r, 3).value: ws.cell(r, 5).value for r in range(2, ws.max_row + 1)}

    for pid in (457, 554, 1537, 1542, 1551, 559, 562):
        assert pid in vals, f"par_id {pid} missing from the output sheet"
        assert isinstance(vals[pid], (int, float)), f"par_id {pid} is {vals[pid]!r}, not a number"

    # Exported values are rounded to 4dp, so a sum of parts can differ from the
    # rounded total in the last place. Exact reconciliation is asserted on the
    # engine values in the test below.
    TOL = 1e-3
    fashion, apparel, footwear, acc = vals[457], vals[554], vals[559], vals[562]
    women, men, kids = vals[1537], vals[1542], vals[1551]
    assert apparel == pytest.approx(women + men + kids, abs=TOL)
    assert footwear == pytest.approx(vals[1547] + vals[1550], abs=TOL)
    assert acc == pytest.approx(vals[1552] + vals[1553] + vals[1554] + vals[1555], abs=TOL)
    assert fashion == pytest.approx(apparel + footwear + acc, abs=TOL)
    assert women == pytest.approx(vals[1538] + vals[1539] + vals[1540] + vals[1541], abs=TOL)
    assert men == pytest.approx(vals[1543] + vals[1544] + vals[1545] + vals[1546], abs=TOL)


def test_fashion_gmv_tree_reconciles_exactly_in_the_engine(run, cfg):
    """Unrounded reconciliation of the same tree, straight off the engine."""
    e = run.estimate
    n = e.num
    assert n(175) == pytest.approx(n(176) + n(181) + n(186), rel=1e-12)
    assert n(176) == pytest.approx(n(177) + n(178) + n(179) + n(180), rel=1e-12)
    assert n(181) == pytest.approx(n(182) + n(183) + n(184) + n(185), rel=1e-12)
    assert n(187) == pytest.approx(n(188) + n(191), rel=1e-12)
    assert n(194) == pytest.approx(n(195) + n(196) + n(197) + n(198), rel=1e-12)
    assert n(174) == pytest.approx(n(175) + n(187) + n(194), rel=1e-12)


def test_fashion_tree_matches_the_segment_total(run, cfg):
    """The L2 rollup and the segment aggregate are two routes to the same number."""
    e = run.estimate
    assert e.num(174) == pytest.approx(e.num(cfg["segments"]["fashion"]["shipped_gmv"]),
                                       rel=1e-9)


def test_nested_rollups_resolve_regardless_of_config_order(run, cfg):
    """Apparel sums rollups that are themselves rollups; none may be left null."""
    e = run.estimate
    for row in (int(k) for k in cfg["l2_rollups"] if k.isdigit()):
        v = e.rows.get(row)
        assert v is not None and not v.is_null, (
            f"rollup row {row} is null; dependency ordering is wrong")


def test_added_parameters_do_not_shadow_the_template(run, cfg):
    ref = run.reference
    added = [t for t in ref.template if t.added]
    original = [t for t in ref.template if not t.added]
    assert len(original) == 158
    assert len(added) == 32
    assert not ({t.par_id for t in added} & {t.par_id for t in original})
    # Nothing invented: every added parameter must exist in the model sheet.
    for t in added:
        assert t.par_id in ref.par_to_row


def test_output_is_ordered_topline_then_categories_then_nulls(run, cfg):
    import openpyxl
    ws = openpyxl.load_workbook(run.outputs["deliverable"])[
        cfg["reference"]["output_template_sheet"][:31]]
    rows = [(ws.cell(r, 3).value, ws.cell(r, 5).value) for r in range(2, ws.max_row + 1)]
    by_id = {t.par_id: t for t in run.reference.template}

    # Every null sits below every populated row.
    last_value = max(i for i, (_, v) in enumerate(rows) if v != "null")
    first_null = min(i for i, (_, v) in enumerate(rows) if v == "null")
    assert first_null > last_value, "null rows must all sit at the bottom"

    def first_row_of(pattern):
        import re
        return min(i for i, (pid, _) in enumerate(rows)
                   if re.search(pattern, by_id[pid].parent_question or ""))

    topline = first_row_of(r"^(GMV|AOV|Topline)$")
    cat_gmv = first_row_of(r"^Category Shipped GMV")
    cat_asp = first_row_of(r"^Category\s+ASP")
    contrib = first_row_of(r"^Category Shipped % GMV")
    assert topline < cat_gmv < cat_asp < contrib


def test_independent_corroboration_of_the_mis(run):
    """The call reproduces the MIS from figures that owe nothing to it."""
    cor = run.corroboration
    assert cor is not None and cor.n >= 2
    by_cat = {c.target: c for c in cor.checks}
    assert abs(by_cat["Personal Care"].deviation) < 0.02
    assert abs(by_cat["Men's Casual Wear"].deviation) < 0.02
    assert cor.corroborated == cor.n
    assert any(f.code == "MIS_CORROBORATION" for f in run.all_findings)


def test_5_missing_sources_redistribute_and_warn(tmp_path, tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("d5"))
    r = run_month("myntra", MONTH, str(REF), str(MIS), transcript_path=None,
                  out_dir=tmp_path, store=store)
    assert r.weights.effective["expert_call"] == 0.0
    assert sum(r.weights.effective.values()) == pytest.approx(1.0)
    assert r.weights.effective["player_mis"] > 0.35
    assert any(f.code == "CALL_MISSING" for f in r.all_findings)
    assert r.weights.confidence.value in ("medium", "low")


def test_5b_missing_mis_is_warned_not_invented(tmp_path, tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("d5b"))
    r = run_month("myntra", MONTH, str(REF), mis_path=None, transcript_path=str(DOC),
                  out_dir=tmp_path, store=store)
    assert any(f.code == "MIS_MISSING" for f in r.all_findings)
    assert r.weights.effective["player_mis"] == 0.0
    shipped = r.estimate.rows[load_player_config("myntra")["platform_rows"]["shipped_gmv"]]
    assert shipped.provenance is not Provenance.REPORTED


def test_6_contradictions_are_flagged_and_explained(run):
    codes = [f.code for f in run.all_findings]
    assert "CALL_MIS_HISTORY_CONFLICT" in codes
    assert "MIS_DEFINITION_CONFLICT" in codes
    conflicts = [f for f in run.all_findings if f.code == "CALL_MIS_HISTORY_CONFLICT"]
    assert all(f.detail for f in conflicts), "every contradiction must carry an explanation"
    assert any("Sports Footwear" in f.message for f in conflicts)


def test_6b_mis_internal_inconsistency_is_an_error(run):
    geo = [f for f in run.all_findings if f.code == "MIS_GEO_SUM"]
    assert geo and geo[0].severity is Severity.ERROR
    assert "1.05" in geo[0].message
    assert "normalised" in geo[0].detail


def test_7_calibration_uses_only_point_in_time_information(run):
    """Backtests must not see the month they estimate."""
    for b in run.calibration.backtests:
        for row in b.per_month:
            assert row["month"] <= MONTH
    assert all(b.n > 0 for b in run.calibration.backtests)


def test_8_reported_actuals_are_not_overwritten(run, cfg, mis):
    e = run.estimate
    assert e.rows[cfg["platform_rows"]["returns_pct"]].provenance is Provenance.REPORTED
    assert e.num(cfg["platform_rows"]["returns_pct"]) == pytest.approx(
        mis.value(MONTH, "Returns", "Overall"))
    gmv = mis.section_values(MONTH, "Category GMV")
    # The category total survives the taxonomy mapping and residual carve.
    assert e.num(cfg["platform_rows"]["shipped_gmv"]) == pytest.approx(
        sum(gmv.values()), rel=1e-9)


def test_9_provenance_stays_distinguishable(run):
    kinds = {v.provenance for v in run.estimate.rows.values()}
    assert Provenance.REPORTED in kinds
    assert Provenance.DERIVED in kinds
    assert Provenance.ESTIMATED in kinds
    assert all(v.method for v in run.estimate.rows.values() if not v.is_null)


def test_9b_estimates_are_never_labelled_reported(run, cfg):
    e = run.estimate
    for key in ("cancellation_pct", "basket_size", "mau", "mac"):
        assert e.rows[cfg["platform_rows"][key]].provenance is not Provenance.REPORTED


def test_10_backtests_report_measured_error(run):
    assert run.calibration.accuracy_measured
    for b in run.calibration.backtests:
        assert b.n >= 10
        assert b.mape is not None and b.mape >= 0
    assert any("not yet available" in s or "no forecast accuracy" in s.lower()
               for s in run.calibration.statements)


def test_10b_no_unearned_accuracy_claim(run):
    joined = " ".join(run.calibration.statements).lower()
    assert "improve" not in joined or "cannot" in joined or "once subsequent" in joined


def test_11_invalid_inputs_give_useful_errors(tmp_path, tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("d11"))
    bad = tmp_path / "not-a-workbook.xlsx"
    bad.write_text("this is not a workbook")
    r = run_month("myntra", MONTH, str(bad), out_dir=tmp_path, store=store)
    assert not r.ok and r.estimate is None
    assert any(f.code == "REF_READ_FAILED" and f.blocking for f in r.findings)
    assert any(f.detail for f in r.errors)


def test_11b_wrong_month_is_rejected(tmp_path, tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("d11b"))
    r = run_month("myntra", "2026-08", str(REF), str(MIS), str(DOC),
                  out_dir=tmp_path, store=store)
    assert any(f.code == "MIS_MONTH_MISMATCH" for f in r.findings)
    assert r.estimate is None


def test_12_runs_are_retained_and_reproducible(run, tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("d12"))
    a = run_month("myntra", MONTH, str(REF), str(MIS), str(DOC),
                  out_dir=tmp_path_factory.mktemp("o1"), store=store)
    b = run_month("myntra", MONTH, str(REF), str(MIS), str(DOC),
                  out_dir=tmp_path_factory.mktemp("o2"), store=store)
    ka = {k: v.value for k, v in a.estimate.rows.items()}
    kb = {k: v.value for k, v in b.estimate.rows.items()}
    assert ka == kb, "two identical runs must produce identical numbers"
    assert len(store.list_runs("myntra")) == 2
    assert store.load_manifest(a.run_id)["month"] == MONTH


def test_residual_categories_are_estimates_not_actuals(run, cfg):
    """Watches and Fashion Accessories have no MIS line, so they cannot be reported."""
    e = run.estimate
    for key in cfg["residual_l2"]["keys"]:
        meta = cfg["l2_categories"][key]
        for field in ("gmv", "asp", "units"):
            v = e.rows[meta[field]]
            assert v.provenance is not Provenance.REPORTED, (
                f"{key}.{field} has no MIS evidence and must not be labelled reported")


def test_residual_units_use_the_unit_share_not_the_gmv_share(run, cfg):
    """These categories carry a high ASP, so their unit share is well below their GMV share."""
    e, ref = run.estimate, run.reference
    prior = "2026-08"
    for key in cfg["residual_l2"]["keys"]:
        meta = cfg["l2_categories"][key]
        hist_asp = ref.value(meta["asp"], prior)
        new_asp = e.num(meta["asp"])
        assert new_asp == pytest.approx(hist_asp, rel=0.25), (
            f"{key} ASP {new_asp:.0f} is far from its {prior} level {hist_asp:.0f}; "
            "units were probably carved on the GMV share")


def test_mis_reported_asp_is_preserved_exactly(run, cfg, mis):
    """A directly mapped category must export the MIS ASP unchanged."""
    asp = mis.section_values(MONTH, "Category ASP")
    e = run.estimate
    for mis_cat, l2 in (("Women's Ethnic", "wom_ethnic"),
                        ("Jewellery", "jewellery"),
                        ("Kids Wear", "kids"),
                        ("Men's Casual Wear", "men_western")):
        got = e.num(cfg["l2_categories"][l2]["asp"])
        assert got == pytest.approx(asp[mis_cat], rel=1e-9), (
            f"{mis_cat} ASP {got} differs from the MIS reported {asp[mis_cat]}")


def test_category_origin_sets_provenance(run, cfg):
    e = run.estimate
    # Direct one-to-one MIS mapping -> reported.
    assert e.rows[cfg["l2_categories"]["wom_ethnic"]["gmv"]].provenance is Provenance.REPORTED
    # Allocated out of a reported total -> derived, not reported.
    assert e.rows[cfg["l2_categories"]["grooming"]["gmv"]].provenance is Provenance.DERIVED
    assert e.rows[cfg["l2_categories"]["home_decor"]["gmv"]].provenance is Provenance.DERIVED


def test_13_nothing_is_fabricated(run, cfg):
    """Parameters with no evidence must be null, never a number."""
    e, ref = run.estimate, run.reference
    unmapped = [t for t in ref.template if t.par_id not in ref.par_to_row]
    assert unmapped, "expected some template parameters with no model row"
    for t in unmapped:
        assert ref.par_to_row.get(t.par_id) is None
    import openpyxl
    ws = openpyxl.load_workbook(run.outputs["deliverable"])[
        cfg["reference"]["output_template_sheet"][:31]]
    nulls = [ws.cell(r, 5).value for r in range(2, ws.max_row + 1)
             if ws.cell(r, 3).value in {t.par_id for t in unmapped}]
    assert nulls and all(v == "null" for v in nulls)


# --------------------------------------------------------------- overrides

def test_override_keeps_the_original_and_requires_a_reason(tmp_path, tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("dov"))
    cfg = load_player_config("myntra")
    row = cfg["platform_rows"]["mau"]
    ov = Override(metric="MAU", row=row, original=None, new_value=72.5,
                  reason="client confirmed a revised MAU", author="analyst")
    r = run_month("myntra", MONTH, str(REF), str(MIS), str(DOC),
                  out_dir=tmp_path, store=store, overrides=[ov])
    v = r.estimate.rows[row]
    assert v.value == pytest.approx(72.5)
    assert v.uncalibrated is not None and v.uncalibrated != 72.5
    assert "manual override" in v.method
    saved = store.load_overrides(r.run_id)
    assert saved and saved[0]["reason"] == "client confirmed a revised MAU"
    assert saved[0]["original"] is not None


def test_override_on_a_reported_actual_is_warned(tmp_path, tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("dov2"))
    cfg = load_player_config("myntra")
    row = cfg["platform_rows"]["returns_pct"]
    ov = Override(metric="Returns", row=row, original=None, new_value=0.28,
                  reason="definition correction agreed with the client")
    r = run_month("myntra", MONTH, str(REF), str(MIS), str(DOC),
                  out_dir=tmp_path, store=store, overrides=[ov])
    assert any(f.code == "OVERRIDE_ON_ACTUAL" for f in r.all_findings)


# -------------------------------------------------------- seasonality lever

def test_seasonality_auto_suppresses_when_the_event_moved(run):
    codes = [f.code for f in run.all_findings]
    assert "SEASONALITY_SUPPRESSED" in codes
    assert "festive event timing shift" in run.estimate.drivers_claimed


def test_forcing_festive_produces_a_higher_estimate(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("dse"))
    cfg = load_player_config("myntra")
    row = cfg["platform_rows"]["basket_size"]
    bau = run_month("myntra", MONTH, str(REF), str(MIS), str(DOC),
                    out_dir=tmp_path_factory.mktemp("ob"), store=store,
                    seasonality_mode="bau")
    fest = run_month("myntra", MONTH, str(REF), str(MIS), str(DOC),
                     out_dir=tmp_path_factory.mktemp("of"), store=store,
                     seasonality_mode="festive")
    assert fest.estimate.num(row) > bau.estimate.num(row)


def test_driver_is_claimed_once(run):
    """The same driver must not be applied through two levers."""
    drivers = list(run.estimate.drivers_claimed)
    assert len(drivers) == len(set(drivers))
    assert "festive event timing shift" in drivers
    assert "calendar seasonality" not in drivers


# ----------------------------------------------------------------- exports

def test_audit_workbook_has_every_required_sheet(run):
    import openpyxl
    wb = openpyxl.load_workbook(run.outputs["audit"])
    for s in ("Summary", "Inputs", "Validation", "Weights", "Call insights",
              "Call caveats", "Metrics", "Corroboration", "Calibration",
              "Backtests", "Adjustments", "Method changes", "Overrides", "Key"):
        assert s in wb.sheetnames, f"audit workbook missing {s}"


def test_audit_calibration_sheet_is_populated(run):
    import openpyxl
    ws = openpyxl.load_workbook(run.outputs["audit"])["Calibration"]
    assert ws.max_row > 10


def test_output_workbook_sheets(run, cfg):
    import openpyxl
    wb = openpyxl.load_workbook(run.outputs["deliverable"])
    assert wb.sheetnames == ["Contents",
                             cfg["reference"]["output_template_sheet"][:31],
                             "Source", "Key"]


def test_output_filenames_are_readable(run):
    """Plain names, no timestamps or id strings."""
    for p in run.outputs.values():
        name = Path(p).name
        assert name.startswith("Myntra Sep 2026 - ")
        assert not any(ch.isdigit() for ch in name.replace("Sep 2026", ""))
    assert Path(run.outputs["deliverable"]).parent.name == "Myntra Sep 2026"


def test_percentages_export_as_template_units(run, cfg):
    import openpyxl
    ws = openpyxl.load_workbook(run.outputs["deliverable"])[
        cfg["reference"]["output_template_sheet"][:31]]
    for r in range(2, ws.max_row + 1):
        if ws.cell(r, 4).value == "%" and isinstance(ws.cell(r, 5).value, (int, float)):
            assert 0 <= ws.cell(r, 5).value <= 100, (
                f"row {r} percentage not scaled to template units")

