"""Uploading a reference each month, the trend/deviation view, and the wrong-file guards."""

from __future__ import annotations

import shutil
from pathlib import Path

import openpyxl
import pytest

from fashion_intel.pipeline import run_month
from fashion_intel.reference_store import default_reference
from fashion_intel.sniff import sniff_file
from fashion_intel.store import Store
from fashion_intel.trend import trend_rows

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures"
NY_MIS, NY_CALL = FIX / "Nykaa Fashion MIS Sep26.xlsx", FIX / "Nykaa Fashion Call Sep26.docx"
MY_MIS = ROOT / "Myntra MIS.xlsx"
MONTH = "2026-09"

pytestmark = pytest.mark.skipif(default_reference() is None, reason="reference missing")


def go(tmp, player="Nykaa Fashion", **kw):
    return run_month(player, MONTH, out_dir=tmp.mktemp("o"), store=Store(tmp.mktemp("d")), **kw)


# ----------------------------------------------------------- content recognition

def test_files_are_recognised_by_content_not_name(tmp_path):
    assert sniff_file(NY_MIS) == "mis"                       # no "MIS" in its real-world name
    odd = tmp_path / "Nykaa fashion sep'26.xlsx"
    shutil.copy(NY_MIS, odd)
    assert sniff_file(odd) == "mis"
    assert sniff_file(NY_CALL) == "transcript"
    assert sniff_file(default_reference()) == "reference"
    renamed = tmp_path / "MIS final.xlsx"
    shutil.copy(default_reference(), renamed)
    assert sniff_file(renamed) == "reference", "a file called MIS can still be a reference"
    assert sniff_file(tmp_path / "x.csv") == "other"


# --------------------------------------------------------------- wrong-file guards

@pytest.mark.skipif(not MY_MIS.exists(), reason="Myntra MIS not present")
def test_another_players_mis_is_stopped_with_a_clear_reason(tmp_path_factory):
    """The failure that produced a 377 Cr 'Nykaa' figure: Myntra's MIS in the MIS slot."""
    r = go(tmp_path_factory, mis_path=str(MY_MIS), transcript_path=str(NY_CALL))
    assert r.estimate is None
    codes = {f.code for f in r.findings if f.blocking}
    assert codes & {"MIS_LAYOUT_MISMATCH", "MIS_LEVEL_MISMATCH"}
    lay = [f for f in r.findings if f.code == "MIS_LAYOUT_MISMATCH"]
    assert lay and "Home Furnishing" in lay[0].message or "Jewellery" in lay[0].message


@pytest.mark.skipif(not MY_MIS.exists(), reason="Myntra MIS not present")
def test_the_standard_mis_given_to_myntra_is_also_stopped(tmp_path_factory):
    r = go(tmp_path_factory, player="Myntra", mis_path=str(NY_MIS))
    assert r.estimate is None
    assert any(f.code in ("MIS_LAYOUT_MISMATCH", "MIS_LEVEL_MISMATCH") for f in r.findings)


def test_a_wrong_scale_is_not_guessed_into_plausibility():
    """Myntra's 3,352 Cr / 10 = 335 'looks' like Nykaa's ~290. It must not be accepted."""
    from fashion_intel.engine.mis_adapter import detect_scale
    assert detect_scale(3352.0, 290.0)[0] == 1.0


# ---------------------------------------------------------------- uploaded reference

def _copy_reference_with(tmp_path, gross_for_month: float):
    """A reference whose Nykaa Sep'26 gross GMV carries a specific previous figure."""
    dst = tmp_path / "Reference with previous numbers.xlsx"
    shutil.copy(default_reference(), dst)
    wb = openpyxl.load_workbook(dst)
    ws = wb["Nykaa Fashion"]
    hdr = next(c for c in range(1, ws.max_column + 1)
               if str(ws.cell(5, c).value).startswith("2026-09") or
               ws.cell(5, c).value == "Sep'26" or
               (hasattr(ws.cell(5, c).value, "month") and ws.cell(5, c).value.month == 9
                and ws.cell(5, c).value.year == 2026))
    row = next(r for r in range(6, ws.max_row + 1) if ws.cell(r, 1).value == 241)
    ws.cell(row, hdr).value = gross_for_month
    wb.save(dst)
    return dst


def test_an_uploaded_reference_is_used_instead_of_the_built_in(tmp_path, tmp_path_factory):
    ref = _copy_reference_with(tmp_path, 199.0)
    r = go(tmp_path_factory, reference_path=str(ref), mis_path=str(NY_MIS),
           transcript_path=str(NY_CALL))
    assert r.estimate is not None
    kinds = [i["kind"] for i in r.manifest["inputs"]]
    assert "reference workbook" in kinds and "built-in reference" not in kinds


def test_the_reference_figure_for_the_month_is_shown_as_a_deviation(tmp_path, tmp_path_factory):
    ref = _copy_reference_with(tmp_path, 199.0)
    r = go(tmp_path_factory, reference_path=str(ref), mis_path=str(NY_MIS),
           transcript_path=str(NY_CALL), history_weight=0.0)
    rows = {t["metric"]: t for t in trend_rows(r.estimate, r.reference, r.config, MONTH)}
    g = rows["Gross GMV"]
    assert g["value"] == pytest.approx(225.21, abs=0.01)
    assert g["reference_figure"] == pytest.approx(199.0)
    assert g["vs_reference"] == pytest.approx(225.2120679 / 199.0 - 1, rel=1e-6)
    assert "differs from reference" in g["flag"]


def test_trend_columns_follow_the_reference_history(tmp_path_factory):
    r = go(tmp_path_factory, mis_path=str(NY_MIS), transcript_path=str(NY_CALL))
    g = {t["metric"]: t for t in trend_rows(r.estimate, r.reference, r.config, MONTH)}["Gross GMV"]
    assert g["prior_month"] == pytest.approx(r.reference.value(r.config["platform_rows"]["gross_gmv"],
                                                               "2026-08"))
    assert g["mom"] == pytest.approx(g["value"] / g["prior_month"] - 1)
    assert g["yoy"] == pytest.approx(g["value"] / g["year_ago"] - 1)


def test_percent_metrics_are_compared_in_points(tmp_path_factory):
    r = go(tmp_path_factory, mis_path=str(NY_MIS), transcript_path=str(NY_CALL))
    c = {t["metric"]: t for t in trend_rows(r.estimate, r.reference, r.config, MONTH)}["Cancellation"]
    assert c["pp"] and c["mom"] == pytest.approx(c["value"] - c["prior_month"])


def test_trend_sheet_is_in_the_audit_workbook(tmp_path_factory):
    r = go(tmp_path_factory, mis_path=str(NY_MIS), transcript_path=str(NY_CALL))
    wb = openpyxl.load_workbook(r.outputs["audit"])
    assert "Trend" in wb.sheetnames
    ws = wb["Trend"]
    labels = [ws.cell(i, 1).value for i in range(5, ws.max_row + 1)]
    assert "Gross GMV" in labels and "Shipped GMV" in labels


def test_saved_history_does_not_override_an_uploaded_references_own_numbers(tmp_path, tmp_path_factory):
    """Save Sep'26 from the built-in, then upload a reference that already has Sep'26."""
    store = Store(tmp_path_factory.mktemp("d"))
    out = tmp_path_factory.mktemp("o")
    run_month("Nykaa Fashion", MONTH, out_dir=out, store=store, save_history=True)
    ref = _copy_reference_with(tmp_path, 199.0)
    r = run_month("Nykaa Fashion", "2026-10", reference_path=str(ref), out_dir=out, store=store)
    P = r.config["platform_rows"]
    assert r.reference.value(P["gross_gmv"], MONTH) == pytest.approx(199.0), \
        "the uploaded reference's own figure must win over a saved run"

