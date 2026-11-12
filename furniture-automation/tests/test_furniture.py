"""Furniture portal tests: discovery, parameter binding, identities and whole-month runs."""

from __future__ import annotations

import json
from pathlib import Path

import openpyxl
import pytest

from furniture_intel.config_resolver import resolve_config
from furniture_intel.discovery import discover_players
from furniture_intel.domain import Severity
from furniture_intel.pipeline import run_month
from furniture_intel.readers.reference import _parse_month_header, load_reference
from furniture_intel.store import Store

ROOT = Path(__file__).resolve().parent.parent
REF = str(ROOT / "reference" / "Furniture Reference.xlsx")
FIX = ROOT / "tests" / "fixtures"
MIS = str(FIX / "Pepperfry MIS Sep26.xlsx")
MONTH = "2026-09"
NEXT = "2026-10"

PLAYERS = ["Pepperfry", "Urban Ladder"]
CATS = [(7101, 1426, 498), (7102, 1427, 499), (7103, 1428, 500), (7104, 1429, 502),
        (7105, 1430, 503)]                      # GMV, share of shipped GMV, ASP

pytestmark = pytest.mark.skipif(not Path(REF).exists(), reason="reference workbook not present")


@pytest.fixture(scope="module")
def players():
    return discover_players(REF)


def _run(player, tmp_path, month=NEXT, **kw):
    return run_month(player, month, reference_path=REF, out_dir=tmp_path / "out",
                     store=Store(tmp_path / "data"), **kw)


def _template():
    return json.loads((ROOT / "furniture_intel" / "config" / "furniture_template.json")
                      .read_text(encoding="utf-8"))["rows"]


# ---------------------------------------------------------------- discovery

def test_both_furniture_players_are_found_and_usable(players):
    assert sorted(p.name for p in players) == sorted(PLAYERS)
    assert all(p.usable for p in players), [p.name for p in players if not p.usable]
    assert all(p.latest_month == MONTH for p in players)


def test_history_starts_at_the_first_clean_month(players):
    """Both sheets interleave Urban Ladder and Pepperfry in alternate columns up to Apr'20;
    reading from there would hand each player the other's figures."""
    assert all(p.months[0] == "2021-04" for p in players)


@pytest.mark.parametrize("text,expected", [
    ("Sep'26", "2026-09"), ("June'26", "2026-06"), ("March'21", "2021-03"), ("Sept'20", "2020-09"),
    ("JFM'18 (monthly averge)", None), ("Oct MOM", None), ("Sep YoY", None),
])
def test_month_headers(text, expected):
    assert _parse_month_header(text) == expected


# -------------------------------------------------------- parameter binding

@pytest.mark.parametrize("name", PLAYERS)
def test_every_template_row_is_bound_and_pars_are_unique(name, players):
    s = next(p for p in players if p.name == name)
    ref = load_reference(REF, resolve_config(name, s, REF))
    rows = _template()
    assert len({r[2] for r in rows}) == len(rows), "a par_id appears twice in the template"
    unbound = [r[1] for r in rows if r[2] not in ref.par_to_row]
    assert not unbound, unbound


@pytest.mark.parametrize("name", PLAYERS)
def test_category_gmv_rows_equal_share_times_shipped_gmv(name, players):
    """Pepperfry keeps par_ids 1426-1430 on the share rows and Urban Ladder on the GMV rows.
    forced_pars.json fixes that; if it breaks, GMV / shipped stops matching the share."""
    s = next(p for p in players if p.name == name)
    ref = load_reference(REF, resolve_config(name, s, REF))
    ship = ref.value_by_par(243, "2026-08")
    for gmv_par, share_par, _asp in CATS[:3]:
        g, sh = ref.value_by_par(gmv_par, "2026-08"), ref.value_by_par(share_par, "2026-08")
        assert g / ship == pytest.approx(sh, abs=0.002), (name, gmv_par)


def test_pepperfrys_rto_and_rvp_come_from_its_helper_rows(players):
    s = next(p for p in players if p.name == "Pepperfry")
    ref = load_reference(REF, resolve_config("Pepperfry", s, REF))
    assert ref.par_to_row[245] == 89 and ref.par_to_row[246] == 90
    assert ref.value_by_par(245, "2026-08") < 0.05


def test_urban_ladders_rto_and_rvp_stay_on_their_own_rows(players):
    s = next(p for p in players if p.name == "Urban Ladder")
    ref = load_reference(REF, resolve_config("Urban Ladder", s, REF))
    assert ref.par_to_row[245] == 17 and ref.par_to_row[246] == 18


def test_template_rows_are_the_furniture_output_layout():
    rows = _template()
    pars = {r[2] for r in rows}
    assert {241, 243, 244, 247, 240, 286, 287, 478, 7501, 7101, 1426, 498, 7110, 7401} <= pars


# ---------------------------------------------------------------- whole runs

@pytest.mark.parametrize("name", PLAYERS)
def test_every_player_runs_with_no_files(name, tmp_path):
    res = _run(name, tmp_path)
    assert res.ok and not res.errors, [e.message for e in res.errors]
    est, ref = res.estimate, res.reference
    assert est.value_for_par(ref, 241) is not None
    gross, canc = est.value_for_par(ref, 241).value, est.value_for_par(ref, 242).value
    ship = est.value_for_par(ref, 243).value
    assert ship == pytest.approx(gross * (1 - canc))


@pytest.mark.parametrize("name,base", [("Pepperfry", "gross"), ("Urban Ladder", "shipped")])
def test_returns_and_net_gmv_follow_each_sheets_formula(name, base, tmp_path):
    res = _run(name, tmp_path)
    est, ref = res.estimate, res.reference
    v = lambda p: est.value_for_par(ref, p).value
    on = v(241) if base == "gross" else v(243)
    assert v(244) == pytest.approx(on * v(7301))
    assert v(247) == pytest.approx(v(243) - v(244))


@pytest.mark.parametrize("name", PLAYERS)
def test_items_asp_and_orders_per_day_identities(name, tmp_path):
    res = _run(name, tmp_path)
    est, ref = res.estimate, res.reference
    v = lambda p: est.value_for_par(ref, p)
    ship = v(243).value
    items = [v(i).value for i in (7111, 7112, 7113, 7114, 7115) if v(i) and v(i).value]
    assert v(7110).value == pytest.approx(sum(items))
    assert v(468).value == pytest.approx(ship * 1e7 / sum(items))
    for gmv_par, share_par, asp_par in CATS:
        g, a = v(gmv_par), v(asp_par)
        if g is not None and g.value and a is not None and a.value:
            assert v(gmv_par + 10).value == pytest.approx(g.value * 1e7 / a.value)
    assert v(7401).value == pytest.approx(ship * 1e7 / v(240).value / 31)
    assert v(4257).value == pytest.approx(v(241).value * 1e7 / v(240).value)
    assert v(238).value == pytest.approx(v(4257).value / 1e6)


@pytest.mark.parametrize("name", PLAYERS)
def test_splits_sum_to_one(name, tmp_path):
    res = _run(name, tmp_path)
    est, ref = res.estimate, res.reference
    v = lambda p: est.value_for_par(ref, p).value
    assert sum(v(p) for p in (223, 224, 225, 226)) == pytest.approx(1.0, abs=1e-6)
    assert v(286) + v(287) == pytest.approx(1.0, abs=1e-6)
    assert v(446) + v(7601) == pytest.approx(1.0, abs=1e-6)
    assert v(7501) + v(478) == pytest.approx(1.0, abs=1e-4)
    shares = [est.value_for_par(ref, s).value for _, s, _ in CATS
              if est.value_for_par(ref, s) is not None and est.value_for_par(ref, s).value]
    assert sum(shares) == pytest.approx(1.0, abs=0.02)


def test_pepperfry_with_a_mis_takes_the_reported_figures(tmp_path):
    res = _run("Pepperfry", tmp_path, month=MONTH, mis_path=MIS)
    assert res.ok and not res.errors, [e.message for e in res.errors]
    est, ref = res.estimate, res.reference
    gross = est.value_for_par(ref, 241)
    assert gross.value == pytest.approx(38.5, rel=1e-6)
    assert gross.provenance.value == "reported"
    assert est.value_for_par(ref, 240).value == pytest.approx(30500.0)
    assert est.value_for_par(ref, 468).value == pytest.approx(12000.0)        # reported, not rebuilt
    # category mix is the MIS mix: furniture 295 of 381.5 Cr
    assert est.value_for_par(ref, 1427).value == pytest.approx(295 / 381.5, rel=1e-4)
    assert est.value_for_par(ref, 499).value == pytest.approx(30000.0)
    ship = est.value_for_par(ref, 243).value
    assert est.value_for_par(ref, 7102).value == pytest.approx(ship * 295 / 381.5, rel=1e-4)


def test_another_players_mis_stops_the_run(tmp_path):
    """Pepperfry's MIS (38.5 Cr gross) given to Urban Ladder (about 21 Cr) must be refused."""
    res = _run("Urban Ladder", tmp_path, month=MONTH, mis_path=MIS)
    assert not res.ok
    assert any(f.severity is Severity.ERROR for f in res.all_findings)


def test_output_sheet_follows_the_template_and_stores_percent_as_fractions(tmp_path):
    res = _run("Pepperfry", tmp_path)
    wb = openpyxl.load_workbook(res.outputs["deliverable"], data_only=True)
    assert wb.sheetnames[:2] == ["Contents", "Furniture-WF"]
    rows = list(wb["Furniture-WF"].iter_rows(min_row=2, values_only=True))
    pars = [int(r[2]) for r in rows if r[2] is not None]
    assert pars[:len(_template())] == [r[2] for r in _template()]
    app = next(r for r in rows if r[2] == 163)
    assert 0 < app[4] < 1                       # 0.76, shown as 76% by the number format


def test_period_blocks_and_edited_analysis_rebuild(tmp_path):
    from furniture_intel.export.from_analysis import rebuild_output
    r = run_month("Pepperfry", NEXT, reference_path=REF, out_dir=tmp_path / "o",
                  store=Store(tmp_path / "d"))
    wb = openpyxl.load_workbook(r.outputs["analysis"])
    ws = wb["Analysis"]
    head = [c.value for c in ws[5]]
    assert "Oct'26" in head and wb["Run data"].sheet_state == "hidden"
    col = [i for i, c in enumerate(ws[5], 1) if c.value == "Oct'26"][-1]
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


# ------------------------------------------------------- channel distribution

MASTER = ROOT / "Furniture_Output_for automation.xlsx"
CH_SHARES = [0.1078, 0.7740, 0.0746, 0.0098, 0.0338]


def _mixes(ws, col):
    g = lambda r: ws.cell(r, col).value
    off = [g(13), g(14), g(15), None, g(17)]
    off[3] = 1 - (off[0] + off[1] + off[2] + off[4])
    on = [g(22), g(23), g(24), g(25), None]
    on[4] = 1 - sum(on[:4])
    return g(6), on, off


def test_channel_workbook_is_built_for_pepperfry_only_and_reconciles(tmp_path):
    res = _run("Pepperfry", tmp_path)
    path = res.outputs["channel"]
    ws = openpyxl.load_workbook(path).active
    col = next(c for c in range(4, 80) if ws.cell(4, c).value == "Oct'26")
    assert ws.cell(4, col + 1).value is None                      # one month added, no more
    o, on, off = _mixes(ws, col)
    assert sum(on) == pytest.approx(1.0, abs=1e-6) and sum(off) == pytest.approx(1.0, abs=1e-6)
    shares = [res.estimate.value_for_par(res.reference, p).value for p in (1426, 1427, 1428, 1429, 1430)]
    for i in range(5):
        assert o * on[i] + (1 - o) * off[i] == pytest.approx(shares[i], abs=2e-4), i
    assert ws.cell(5, col).value == pytest.approx(res.estimate.value_for_par(res.reference, 243).value,
                                                  rel=1e-6)
    assert not any(f.code == "CHANNEL_NOT_BUILT" for f in res.all_findings)
    assert "channel" not in _run("Urban Ladder", tmp_path).outputs      # online only: no such sheet


def test_channel_workbook_keeps_the_reference_format(tmp_path):
    from furniture_intel.export import channel as ch
    out = tmp_path / "c.xlsx"
    ch.build_channel_workbook(MASTER, "2026-10", 37.3, CH_SHARES, out)
    src = openpyxl.load_workbook(MASTER)[ch.SHEET]
    ws = openpyxl.load_workbook(out).active
    assert openpyxl.load_workbook(out).sheetnames == [ch.SHEET]
    assert ws.freeze_panes == src.freeze_panes == "C1"
    assert ws.sheet_view.showGridLines is False and ws.sheet_view.zoomScale == src.sheet_view.zoomScale
    assert ws.column_dimensions["D"].hidden and not ws.column_dimensions["BG"].hidden
    # the new column is styled like the last real month, row for row
    for r in (4, 5, 6, 7, 12, 13, 16, 21, 22, 26):
        a, b = src.cell(r, 69), ws.cell(r, 70)                    # Sep'26 -> Oct'26
        assert (a.number_format, a.font.b, a.border.left.style, a.fill.fill_type) == \
               (b.number_format, b.font.b, b.border.left.style, b.fill.fill_type), r
    assert ws["BR16"].value == "=1-SUM(BR13:BR15,BR17)" and ws["BR7"].value == "=1-BR6"
    assert not any(isinstance(c.value, str) and "!" in c.value for row in ws.iter_rows() for c in row)


def test_every_working_block_formula_still_points_at_its_own_month(tmp_path):
    """Inserting a month into six places must not leave any formula reading another month's column.

    Checked by position, not header text: the source sheet's own headers carry typos (its Total
    block labels the Jun'26 column "May'26"). The blocks and the table run month for month."""
    from furniture_intel.export import channel as ch
    from openpyxl.utils import column_index_from_string as ci
    out = tmp_path / "c.xlsx"
    ch.build_channel_workbook(MASTER, "2026-10", 37.3, CH_SHARES, out)
    ws = openpyxl.load_workbook(out).active
    table = {_parse_month_header(ws.cell(4, c).value): c for c in range(4, 80) if ws.cell(4, c).value}
    blocks = ch._blocks(ws)
    assert len({b["last"] - b["first"] for b in blocks}) == 1 and len(blocks) == 5
    first_table_col = table["2024-06"]
    checked = 0
    for b in blocks:
        for c in range(b["first"], b["last"] + 1):
            f = ws.cell(13, c).value
            if not (isinstance(f, str) and f.startswith("=")):
                continue
            k = c - b["first"]
            for m in ch._REF.finditer(f):
                col = ci(m.group(2))
                if col <= max(table.values()):                    # the main table
                    assert col == first_table_col + k, (b["name"], c, f)
                else:
                    host = next(x for x in blocks if x["first"] <= col <= x["last"])
                    assert col - host["first"] == k, (b["name"], c, f)
                checked += 1
    assert checked > 100


def test_channel_months_chain_and_gaps_are_refused(tmp_path):
    from furniture_intel.export import channel as ch
    a, b = tmp_path / "oct.xlsx", tmp_path / "nov.xlsx"
    est = ch.build_channel_workbook(MASTER, "2026-10", 37.3, CH_SHARES, a)
    ch.build_channel_workbook(a, "2026-11", 38.0, CH_SHARES, b)
    ws = openpyxl.load_workbook(b).active
    assert [ws.cell(4, c).value for c in (69, 70, 71)] == ["Sep'26", "Oct'26", "Nov'26"]
    assert ws.cell(6, 70).value == pytest.approx(est["online"], abs=1e-4)      # October kept as it was
    with pytest.raises(ValueError):
        ch.build_channel_workbook(MASTER, "2026-12", 38.0, CH_SHARES, tmp_path / "x.xlsx")


def test_history_saved_run_becomes_the_next_months_channel_source(tmp_path):
    r1 = _run("Pepperfry", tmp_path, save_history=True)
    r2 = _run("Pepperfry", tmp_path, month="2026-11")
    ws = openpyxl.load_workbook(r2.outputs["channel"]).active
    assert [ws.cell(4, c).value for c in (70, 71)] == ["Oct'26", "Nov'26"]
