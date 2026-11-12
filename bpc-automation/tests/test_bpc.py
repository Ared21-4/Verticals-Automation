"""BPC portal tests: discovery, parameter binding, the MIS roll-up and whole-month runs."""

from __future__ import annotations

import json
from pathlib import Path

import openpyxl
import pytest

from bpc_intel.config_resolver import resolve_config
from bpc_intel.discovery import discover_players
from bpc_intel.domain import Finding, Severity
from bpc_intel.engine.mis_adapter import MisCategory, _roll_up
from bpc_intel.pipeline import run_month
from bpc_intel.readers.reference import _parse_month_header, load_reference
from bpc_intel.store import Store

ROOT = Path(__file__).resolve().parent.parent
REF = str(ROOT / "reference" / "BPC Reference.xlsx")
FIX = ROOT / "tests" / "fixtures"
MIS = str(FIX / "Nykaa BPC MIS Sep26.xlsx")
CALL = str(FIX / "Nykaa BPC Call Sep26.docx")
MONTH = "2026-09"

PLAYERS = ["Nykaa", "Purplle", "Mamaearth", "Sugar", "Mcaffeine", "Beardo", "Plum", "Sephora",
           "WOW", "MAC", "MyGlamm"]

pytestmark = pytest.mark.skipif(not Path(REF).exists(), reason="reference workbook not present")


@pytest.fixture(scope="module")
def players():
    return discover_players(REF)


def _run(player, tmp_path, **kw):
    return run_month(player, MONTH, reference_path=REF, out_dir=tmp_path / "out",
                     store=Store(tmp_path / "data"), **kw)


# ---------------------------------------------------------------- discovery

def test_all_eleven_bpc_players_are_found_and_usable(players):
    assert sorted(p.name for p in players) == sorted(PLAYERS)
    assert all(p.usable for p in players), [p.name for p in players if not p.usable]
    assert all(p.latest_month == MONTH for p in players)


def test_sheets_with_a_blank_par_id_header_are_found(players):
    # Sephora, Beardo and WOW leave the par_id header cell blank or hold a stray character.
    names = {p.name for p in players}
    assert {"Sephora", "Beardo", "WOW"} <= names


@pytest.mark.parametrize("text,expected", [
    ("Sep'26", "2026-09"), ("June'22", "2022-06"), ("Sep 24'", "2024-09"),
    ("Dec'22 (Big Bold Sale)", "2022-12"), ("Sep MoM", None), ("JAS'25 (GMV @ MRP)", None),
])
def test_month_headers(text, expected):
    assert _parse_month_header(text) == expected


# -------------------------------------------------- binding rows with no par_id

@pytest.mark.parametrize("name", PLAYERS)
def test_category_gmv_rows_bound_by_label_match_the_shares(name, players):
    """Rows the sheets leave without a par_id are bound by label. Check the binding is right:
    each category's GMV over shipped GMV must equal the category's share row."""
    s = next(p for p in players if p.name == name)
    ref = load_reference(REF, resolve_config(name, s, REF))
    ship = ref.value_by_par(243, "2026-08")
    if not ship:
        pytest.skip("no shipped GMV history")
    for gmv_par, share_par in ((249, 1580), (250, 1581), (4571, 1583)):
        g, sh = ref.value_by_par(gmv_par, "2026-08"), ref.value_by_par(share_par, "2026-08")
        if g is None or sh is None:
            continue
        assert g / ship == pytest.approx(sh, abs=0.002), (name, gmv_par)


def test_template_is_the_delivered_178_rows():
    rows = json.loads((ROOT / "bpc_intel" / "config" / "bpc_template.json")
                      .read_text(encoding="utf-8"))["rows"]
    assert len(rows) == 178
    pars = {r[2] for r in rows}
    assert {241, 242, 240, 238, 1580, 1581, 1583, 492, 493, 494, 286, 287, 476, 477, 478} <= pars


# ------------------------------------------------------------- MIS roll-up

def test_nine_mis_lines_roll_up_into_three_categories():
    g = {"gmv": 249, "asp": 492, "share": 1580}
    cats = [MisCategory("Body Skin", 12.0, 450.0, g), MisCategory("Face Skin", 20.0, 650.0, g),
            MisCategory("Make up", 21.0, 700.0, {"gmv": 250, "asp": 493, "share": 1581})]
    out = _roll_up(cats, [])
    assert [c.pars["share"] for c in out] == [1580, 1581]
    groom = out[0]
    assert groom.gmv_cr == pytest.approx(32.0)
    # ASP is combined GMV over combined units, not a plain average of the two ASPs
    units = 12 * 10 / 450 + 20 * 10 / 650
    assert groom.asp == pytest.approx(32 * 10 / units)
    assert out[1].asp == 700.0


def test_a_line_without_asp_is_reported_not_hidden():
    g = {"gmv": 249, "asp": 492, "share": 1580}
    f: list[Finding] = []
    out = _roll_up([MisCategory("Body Skin", 10.0, 400.0, g),
                    MisCategory("Hygiene", 10.0, None, g)], f)
    assert out[0].gmv_cr == 20.0 and out[0].asp == 400.0
    assert any(x.code == "MIS_ASP_PARTIAL" for x in f)


# ---------------------------------------------------------------- whole runs

def test_nykaa_with_mis_and_call(tmp_path):
    res = _run("Nykaa", tmp_path, mis_path=MIS, transcript_path=CALL)
    assert res.ok and not res.errors
    est, ref = res.estimate, res.reference
    gross = est.value_for_par(ref, 241)
    assert gross.value == pytest.approx(810.0, rel=1e-6)
    assert gross.provenance.value == "reported"

    shares = [est.value_for_par(ref, p).value for p in (1580, 1581, 1583)]
    assert sum(shares) == pytest.approx(1.0, abs=1e-6)
    assert shares[0] == pytest.approx(5.1 / 8.1, abs=1e-6)             # six lines -> Grooming
    assert est.value_for_par(ref, 493).value == pytest.approx(800.0)   # Make up + Fragrance
    ship = est.value_for_par(ref, 243).value
    assert est.value_for_par(ref, 249).value == pytest.approx(shares[0] * ship)

    # the BPC category rows repeat the platform rows
    for target, source in ((3612, 238), (4487, 287), (4499, 286),
                           (4511, 476), (4523, 477), (4535, 478)):
        assert est.value_for_par(ref, target).value == pytest.approx(
            est.value_for_par(ref, source).value), target


def test_output_sheet_keeps_the_delivered_order(tmp_path):
    res = _run("Nykaa", tmp_path, mis_path=MIS, transcript_path=CALL)
    wb = openpyxl.load_workbook(res.outputs["deliverable"], data_only=True)
    ws = wb["BPC-WF"]
    pars = [int(r[2]) for r in ws.iter_rows(min_row=2, values_only=True) if r[2] is not None]
    tpl = [r[2] for r in json.loads((ROOT / "bpc_intel" / "config" / "bpc_template.json")
                                    .read_text(encoding="utf-8"))["rows"]]
    assert pars[:178] == tpl
    assert len(pars) > 178                       # the model's additions follow, in their own block


@pytest.mark.parametrize("name", PLAYERS)
def test_every_player_runs_with_no_files(name, tmp_path):
    res = _run(name, tmp_path)
    assert res.ok and not res.errors, [e.message for e in res.errors]
    assert res.estimate.value_for_par(res.reference, 241) is not None


def test_another_players_mis_stops_the_run(tmp_path):
    """Nykaa's MIS (810 Cr) given to MAC (about 5 Cr) must be refused, not turned into numbers."""
    res = _run("MAC", tmp_path, mis_path=MIS)
    assert not res.ok
    assert any(f.severity is Severity.ERROR for f in res.all_findings)


def test_period_blocks_deviation_detail_and_edited_analysis_rebuild(tmp_path):
    from bpc_intel.export.from_analysis import rebuild_output
    r = run_month("Nykaa", MONTH, reference_path=REF, out_dir=tmp_path / "o", store=Store(tmp_path / "d"))
    wb = openpyxl.load_workbook(r.outputs["analysis"])
    ws = wb["Analysis"]
    head = [c.value for c in ws[5]]
    assert "Q2 FY27" in head and "H1 FY27" in head and "Q2 FY27 vs Q2 FY26" in head
    assert wb["Run data"].sheet_state == "hidden"
    dev = wb["Deviation"]
    hdr = next(row for row in dev.iter_rows() if row[0].value == "Parameter")
    assert [c.value for c in hdr[9:15]] == ["MIS", "Ours", "Difference", "MIS", "Ours", "Difference"]
    col = [i for i, c in enumerate(ws[5], 1) if c.value == "Sep'26"][-1]
    row = next(x for x in ws.iter_rows(min_row=6) if isinstance(x[0].value, int)
               and x[col - 1].value is not None and x[3].value != "%")
    par = row[0].value
    row[col - 1].value = 4321.0
    edited = tmp_path / "edited.xlsx"
    wb.save(edited)
    out, info = rebuild_output(edited, tmp_path / "o")
    assert str(out) == r.outputs["deliverable"] and info["edited"] == 1
    o = openpyxl.load_workbook(out)
    src = next(x for x in o["Source"].iter_rows(min_row=5) if x[0].value == par)
    assert src[2].value == 4321.0 and src[3].value == "Assumed"


def test_the_analysis_flags_trend_changes_for_review(tmp_path):
    import openpyxl
    r = run_month("Nykaa", MONTH, reference_path=REF, out_dir=tmp_path / "o", store=Store(tmp_path / "d"))
    ws = openpyxl.load_workbook(r.outputs["analysis"])["Analysis"]
    head = [c.value for c in ws[5]]
    m, y = head.index("MoM trend") + 1, head.index("YoY trend") + 1
    assert y == m + 1 and str(ws.cell(3, 1).value).startswith('="Trend changes this month')
    gross = next(row for row in ws.iter_rows(min_row=6) if row[0].value == 241)
    f = gross[m - 1].value
    assert f.startswith("=IF(AND(ISNUMBER(") and '"Turned up"' in f and '"Slowing"' in f
    assert '"YoY turned negative"' in gross[y - 1].value
    assert ws.auto_filter.ref and ws.conditional_formatting
