"""Running several players at once, changing MoM and YoY, and the brand-level weight."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from fashion_intel.batch import default_batch, match_player, run_batch, zip_name
from fashion_intel.engine.weights import resolve_weights
from fashion_intel.pipeline import load_default_weights, run_month
from fashion_intel.reference_store import default_reference
from fashion_intel.store import Store

FIX = Path(__file__).parent / "fixtures"
MIS, CALL = FIX / "Nykaa Fashion MIS Sep26.xlsx", FIX / "Nykaa Fashion Call Sep26.docx"
MONTH = "2026-09"
need_ref = pytest.mark.skipif(default_reference() is None, reason="reference missing")
need_fix = pytest.mark.skipif(default_reference() is None or not MIS.exists(), reason="inputs missing")


# ------------------------------------------------------------- matching files to players

@pytest.mark.parametrize("name,player", [
    ("Zara MIS Sep.xlsx", "Zara"), ("H&M_sep26.xlsx", "H&M"), ("Zivamae call.docx", "Zivame"),
    ("Marks and Spencer.xlsx", "M&S"), ("M&S MIS.xlsx", "M&S"),
    ("Nykaa fashion sep'26.xlsx", "Nykaa Fashion"), ("Shopperstop MIS.xlsx", "Shoppers Stop"),
    ("Lime Road call.docx", "Limeroad"), ("Bewakoof_Sep.xlsx", "Bewakoof"),
    ("Clovia.xlsx", "Clovia"), ("Myntra MIS.xlsx", "Myntra"), ("random.xlsx", None)])
def test_files_are_matched_to_players_by_name(name, player):
    assert match_player(name) == player


def test_the_default_batch_is_the_eight_players_named():
    assert default_batch() == ["Bewakoof", "H&M", "Limeroad", "Clovia", "M&S",
                               "Shoppers Stop", "Zara", "Zivame"]


# ------------------------------------------------------------------------ batch run

@pytest.fixture(scope="module")
def batch(tmp_path_factory):
    return run_batch(["Zara", "NoSuchPlayer", "H&M"], MONTH, out_dir=tmp_path_factory.mktemp("o"),
                     store=Store(tmp_path_factory.mktemp("d")))


@need_ref
def test_one_player_failing_does_not_stop_the_others(batch):
    status = {i.player: i.status for i in batch.items}
    assert status["Zara"] == "done" and status["H&M"] == "done"
    assert status["NoSuchPlayer"] in ("stopped", "error")
    assert batch.done == 2


@need_ref
def test_each_player_has_its_own_folder_and_a_summary_row(batch):
    rows = {r["player"]: r for r in batch.rows()}
    assert rows["Zara"]["gross GMV"] > 0 and rows["H&M"]["gross GMV"] > 0
    assert rows["Zara"]["files used"] == "history only"
    assert Path(next(i for i in batch.items if i.player == "Zara").folder).name.startswith("Zara")
    assert Path(next(i for i in batch.items if i.player == "H&M").folder).name.startswith("H&M")
    assert "NoSuchPlayer" in rows and rows["NoSuchPlayer"]["message"]


@need_ref
def test_a_zip_holds_every_player_in_its_own_folder(batch, tmp_path):
    z = batch.zip_to(tmp_path / zip_name(MONTH))
    names = zipfile.ZipFile(z).namelist()
    assert zip_name(MONTH) == "All players Sep 2026.zip"
    assert any(n.startswith("Zara/") and n.endswith("Output.xlsx") for n in names)
    assert any(n.startswith("H&M/") and n.endswith("Audit.xlsx") for n in names)
    assert not any(n.startswith("NoSuchPlayer") for n in names)


@need_fix
def test_a_players_own_files_are_used_and_a_wrong_file_stops_only_that_player(tmp_path_factory):
    files = {"Nykaa Fashion": {"mis": str(MIS), "transcript": str(CALL)},
             "Clovia": {"mis": str(MIS)}}                       # Nykaa's MIS given to Clovia
    b = run_batch(["Nykaa Fashion", "Clovia", "Zara"], MONTH, files_by_player=files,
                  out_dir=tmp_path_factory.mktemp("o"), store=Store(tmp_path_factory.mktemp("d")))
    st = {i.player: i for i in b.items}
    assert st["Nykaa Fashion"].status == "done"
    assert st["Clovia"].status == "stopped" and "another player" in st["Clovia"].message \
        or st["Clovia"].status == "stopped"
    assert st["Zara"].status == "done"
    assert any(r["player"] == "Nykaa Fashion" and r["files used"] == "mis, transcript"
               for r in b.rows())


# ----------------------------------------------------------- changing MoM and YoY

def nyk(tmp, **kw):
    return run_month("Nykaa Fashion", MONTH, mis_path=str(MIS), transcript_path=str(CALL),
                     out_dir=tmp.mktemp("o"), store=Store(tmp.mktemp("d")), **kw)


@need_fix
def test_changing_mom_and_yoy_changes_the_output(tmp_path_factory):
    base = nyk(tmp_path_factory)
    low = nyk(tmp_path_factory, growth_override={"mom": -0.05, "yoy": 0.10})
    high = nyk(tmp_path_factory, growth_override={"mom": 0.05, "yoy": 0.45})
    g = lambda r: r.estimate.value_for_par(r.reference, 241).value
    assert g(low) < g(base) < g(high)
    rc = high.estimate.reconciliations[241]
    assert rc.mom_stated == 0.05 and rc.yoy_stated == 0.45, "the override replaces the MIS's rates"


@need_fix
def test_the_change_flows_through_to_everything_built_from_gross_gmv(tmp_path_factory):
    a = nyk(tmp_path_factory, growth_override={"mom": 0.0, "yoy": 0.10})
    b = nyk(tmp_path_factory, growth_override={"mom": 0.0, "yoy": 0.50})
    for par in (243, 247, 457):
        va, vb = (r.estimate.value_for_par(r.reference, par).value for r in (a, b))
        assert vb > va, f"par {par} did not follow the growth setting"


@need_fix
def test_the_override_can_be_cleared_to_get_the_mis_figures_back(tmp_path_factory):
    base = nyk(tmp_path_factory)
    again = nyk(tmp_path_factory, growth_override=None)
    g = lambda r: r.estimate.value_for_par(r.reference, 241).value
    assert g(base) == pytest.approx(g(again))


@need_ref
def test_growth_settings_work_without_a_mis_too(tmp_path_factory):
    r = run_month("Zara", MONTH, out_dir=tmp_path_factory.mktemp("o"),
                  store=Store(tmp_path_factory.mktemp("d")),
                  growth_override={"mom": 0.0, "yoy": 0.15})
    rc = r.estimate.reconciliations.get(241)
    assert rc is not None and rc.yoy_stated == 0.15 and rc.balanced_level is not None


@need_ref
def test_history_weight_zero_ignores_the_override(tmp_path_factory):
    r = run_month("Zara", MONTH, out_dir=tmp_path_factory.mktemp("o"),
                  store=Store(tmp_path_factory.mktemp("d")), history_weight=0.0,
                  growth_override={"mom": 0.0, "yoy": 0.5})
    plain = run_month("Zara", MONTH, out_dir=tmp_path_factory.mktemp("o"),
                      store=Store(tmp_path_factory.mktemp("d")), history_weight=0.0)
    g = lambda x: x.estimate.value_for_par(x.reference, 241).value
    assert g(r) == pytest.approx(g(plain))


MYN = Path(__file__).resolve().parent.parent
MYN_MIS, MYN_DOC = MYN / "Myntra MIS.xlsx", MYN / "Myntra_September_Expert_Call_Insights.docx"


@pytest.mark.skipif(default_reference() is None or not MYN_MIS.exists(), reason="inputs missing")
def test_myntra_growth_setting_moves_gross_and_shipped_follows(tmp_path_factory):
    def go(**kw):
        return run_month("myntra", MONTH, mis_path=str(MYN_MIS), transcript_path=str(MYN_DOC),
                         out_dir=tmp_path_factory.mktemp("o"), store=Store(tmp_path_factory.mktemp("d")), **kw)
    base, hi = go(), go(growth_override={"mom": 0.0, "yoy": 0.60})
    v = lambda r, p: r.estimate.value_for_par(r.reference, p).value
    assert hi.estimate.reconciliations[241].yoy_stated == 0.60
    assert v(hi, 241) > v(base, 241)
    assert v(hi, 243) / v(base, 243) == pytest.approx(v(hi, 241) / v(base, 241), rel=1e-6)


# ------------------------------------------------------------- edits in the metrics table

@need_ref
def test_a_table_edit_replaces_the_figure_wherever_the_metric_lives(tmp_path_factory):
    from fashion_intel.store import Override
    ov = [Override("AOV", None, None, 4000.0, "test", "t", par=240, unit="INR"),       # no sheet row
          Override("MAU", None, None, 2.5, "test", "t", par=52, unit="Mn")]            # a sheet row
    r = run_month("Zara", MONTH, out_dir=tmp_path_factory.mktemp("o"),
                  store=Store(tmp_path_factory.mktemp("d")), overrides=ov)
    assert r.estimate.value_for_par(r.reference, 240).value == 4000.0
    assert r.estimate.value_for_par(r.reference, 52).value == 2.5
    assert r.estimate.value_for_par(r.reference, 52).provenance.value == "assumed"
    assert ov[0].original is not None, "the original is kept for the audit"


# ------------------------------------------------------------- deviation and comparison files

@need_fix
def test_the_deviation_workbook_sets_the_output_against_the_mis_and_the_call(tmp_path_factory):
    import openpyxl
    r = run_month("Nykaa Fashion", MONTH, mis_path=str(MIS), transcript_path=str(CALL),
                  out_dir=tmp_path_factory.mktemp("o"), store=Store(tmp_path_factory.mktemp("d")))
    wb = openpyxl.load_workbook(r.outputs["analysis"])
    assert "deviation" not in r.outputs, "the deviation sheets live inside the analysis workbook"
    assert wb.sheetnames[:4] == ["Analysis", "Deviation", "Vs MIS", "Vs expert call"]
    ws = wb["Vs MIS"]
    gross = next(row for row in ws.iter_rows(min_row=5) if row[0].value == 241)
    assert gross[3].value == pytest.approx(225.21, abs=0.01), "the MIS figure is kept beside the output"
    assert gross[4].value > gross[3].value and str(gross[6].value).startswith("=IFERROR(E")
    assert ws.conditional_formatting, "deviations are coloured"
    stated = [row[0].value for row in wb["Vs expert call"].iter_rows()]
    assert "Platform GMV" in stated


@need_ref
def test_the_deviation_workbook_says_so_when_there_is_no_mis(tmp_path_factory):
    import openpyxl
    r = run_month("Zara", MONTH, out_dir=tmp_path_factory.mktemp("o"),
                  store=Store(tmp_path_factory.mktemp("d")))
    wb = openpyxl.load_workbook(r.outputs["analysis"])
    text = " ".join(str(c.value) for row in wb["Vs MIS"].iter_rows() for c in row if c.value)
    assert "No MIS was supplied" in text


@need_ref
def test_a_batch_writes_one_workbook_comparing_every_player(batch):
    import openpyxl
    assert batch.collated and Path(batch.collated).exists()
    wb = openpyxl.load_workbook(batch.collated)
    assert wb.sheetnames == ["Summary", "Values", "MoM", "YoY", "Sources"]
    head = [c.value for c in wb["Values"][4]]
    assert head[:3] == ["par_id", "Parameter", "Unit"] and "Zara" in head and "H&M" in head
    assert wb["MoM"].conditional_formatting and wb["Values"].conditional_formatting
    gross = next(row for row in wb["Values"].iter_rows(min_row=5) if row[0].value == 241)
    assert all(c.value and c.value > 0 for c in gross[3:5])


# ------------------------------------------------------------- quarterly, half-yearly, red-yellow-green

@need_ref
def test_the_analysis_has_quarterly_and_half_yearly_views_in_red_yellow_green(tmp_path_factory):
    import openpyxl
    r = run_month("Zara", MONTH, out_dir=tmp_path_factory.mktemp("o"), store=Store(tmp_path_factory.mktemp("d")))
    wb = openpyxl.load_workbook(r.outputs["analysis"])
    assert wb.sheetnames[-3:] == ["Quarterly", "Half-yearly", "Run data"]
    assert wb["Run data"].sheet_state == "hidden"
    q, h = wb["Quarterly"], wb["Half-yearly"]
    qh, hh = [c.value for c in q[5]], [c.value for c in h[5]]
    assert qh[5:17][-1] == "Q2 FY27" and "Q2 FY27 vs Q1 FY27" in qh and "Q2 FY27 vs Q2 FY26" in qh
    assert hh[5:11][-1] == "H1 FY27" and "H1 FY27 vs H2 FY26" in hh and "H1 FY27 vs H1 FY26" in hh
    gross = next(row for row in q.iter_rows(min_row=6) if row[0].value == 241)
    assert gross[4].value == "Sum" and str(gross[16].value).startswith("=IF(COUNT(History!")
    # a rate is averaged, not summed
    rto = next(row for row in q.iter_rows(min_row=6) if row[0].value == 245)
    assert rto[4].value == "Avg" and "AVERAGE(" in str(rto[16].value)
    for ws in (wb["Analysis"], q, h, wb["History MoM"], wb["History YoY"]):
        rules = [r for cf in ws.conditional_formatting for r in cf.rules]
        assert rules, ws.title
        for rule in rules:
            if rule.type != "colorScale":        # the Analysis trend-change highlight is a plain formula rule
                continue
            cs = rule.colorScale
            assert [c.rgb[-6:] for c in cs.color] == ["F8696B", "FFEB84", "63BE7B"], ws.title


# ------------------------------------------------------------- Snapdeal and Tata CLiQ

PLAYER_BOOKS = Path(r"C:\Users\AdityaKrishnanNair\Downloads\Fashion automation")


def test_numbers_typed_as_text_are_read_as_numbers():
    from fashion_intel.readers.reference import _num
    assert _num("1,625") == 1625.0 and _num(" 119 ") == 119.0 and _num(3) == 3.0
    assert _num("n/a") is None and _num("12%") is None and _num(None) is None and _num(True) is None


@need_ref
@pytest.mark.parametrize("name,book,gross", [("Snapdeal", "Snapdeal_product.xlsx", (100, 180)),
                                             ("Tata CLiQ", "Tata Cliq_product.xlsx", (90, 160))])
def test_a_players_own_workbook_works_as_the_reference(name, book, gross, tmp_path_factory):
    p = PLAYER_BOOKS / book
    if not p.exists():
        pytest.skip("player workbook not present")
    from fashion_intel.sniff import sniff_file
    assert sniff_file(p) == "reference" and match_player(book) == name
    r = run_month(name, MONTH, reference_path=str(p), out_dir=tmp_path_factory.mktemp("o"),
                  store=Store(tmp_path_factory.mktemp("d")))
    assert r.estimate is not None and not r.errors
    g = r.estimate.value_for_par(r.reference, 241).value
    assert gross[0] < g < gross[1]
    units = r.reference.row_units
    assert units[r.reference.par_to_row[241]] == "INR Cr", "units come from the name when there is no units column"
    assert len(r.reference.template) > 100, "the output template comes from the built-in reference"


@need_ref
def test_a_batch_gives_each_player_the_reference_that_holds_it(tmp_path_factory):
    s, t = PLAYER_BOOKS / "Snapdeal_product.xlsx", PLAYER_BOOKS / "Tata Cliq_product.xlsx"
    if not (s.exists() and t.exists()):
        pytest.skip("player workbooks not present")
    b = run_batch(["Snapdeal", "Tata CLiQ", "Zara"], MONTH, reference_path=[str(s), str(t)],
                  out_dir=tmp_path_factory.mktemp("o"), store=Store(tmp_path_factory.mktemp("d")))
    assert b.done == 3
    used = {i.player: {x["kind"] for x in i.result.manifest["inputs"]} for i in b.items}
    assert "reference workbook" in used["Snapdeal"] and "reference workbook" in used["Tata CLiQ"]
    assert "built-in reference" in used["Zara"], "a player neither workbook holds uses the built-in history"


@need_ref
def test_a_category_total_matches_its_parts_where_the_sheet_stores_both(tmp_path_factory):
    r = run_month("Tata CLiQ", MONTH, out_dir=tmp_path_factory.mktemp("o"),
                  store=Store(tmp_path_factory.mktemp("d")))
    v = lambda p: r.estimate.value_for_par(r.reference, p).value
    assert v(554) == pytest.approx(v(1537) + v(1542) + v(1551), abs=1e-6)
    assert v(457) == pytest.approx(v(554) + v(559) + v(562), abs=1e-6)


# ------------------------------------------------------------- history inside the analysis, and collating

@need_ref
def test_the_analysis_carries_the_whole_history_and_collates_across_players(tmp_path_factory):
    import openpyxl
    from fashion_intel.export.collate import collate_analysis_files
    od, st = tmp_path_factory.mktemp("o"), Store(tmp_path_factory.mktemp("d"))
    runs = [run_month(p, MONTH, out_dir=od, store=st) for p in ("Zara", "H&M")]
    wb = openpyxl.load_workbook(runs[0].outputs["analysis"])
    assert wb.sheetnames == ["Analysis", "Deviation", "Vs MIS", "Vs expert call", "History", "History MoM",
                             "History YoY", "Quarterly", "Half-yearly", "Run data"]
    hist = wb["History"]
    head = [c.value for c in hist[4]]
    assert head[4] == "Jan'22" and head[-1] == "Sep'26", "every stored month, up to this one"
    gross = next(r for r in hist.iter_rows(min_row=5) if r[0].value == 241)
    assert sum(isinstance(c.value, (int, float)) for c in gross[4:]) > 40
    assert str(next(r for r in wb["History YoY"].iter_rows(min_row=5) if r[0].value == 241)[-1].value).startswith("=")

    out, problems = collate_analysis_files([r.outputs["analysis"] for r in runs] + [runs[0].outputs["audit"]],
                                           od / "all.xlsx")
    names = openpyxl.load_workbook(out).sheetnames
    assert names[:4] == ["Summary", "Compare values", "Compare MoM", "Compare YoY"]
    assert len(names) == 4 + 2, "one tab per player (values, MoM and YoY on it)"
    assert any("not used" in p for p in problems), "a file that is not an analysis is reported, not fatal"


# ------------------------------------------------------------- the analysis workbook

@need_ref
def test_the_analysis_workbook_lays_the_month_beside_its_past_and_last_year(tmp_path_factory):
    import openpyxl
    r = run_month("Zara", MONTH, out_dir=tmp_path_factory.mktemp("o"),
                  store=Store(tmp_path_factory.mktemp("d")))
    ws = openpyxl.load_workbook(r.outputs["analysis"])["Analysis"]
    head = [c.value for c in ws[5]]
    assert head[:4] == ["par_id", "S.No", "Parameter", "Units"]
    assert head[4:10] == ["Apr'25", "May'25", "Jun'25", "Jul'25", "Aug'25", "Sep'25"]
    assert head[11:17] == ["Apr'26", "May'26", "Jun'26", "Jul'26", "Aug'26", "Sep'26"]
    assert "Sep'25" in head and "Sep'26 vs Sep'25" in head and "Sep'26 vs Aug'26" in head
    gross = next(row for row in ws.iter_rows(min_row=6) if row[0].value == 241)
    assert gross[16].value and gross[16].value > 0, "this month's figure sits in the last recent column"
    assert any(isinstance(c.value, str) and c.value.startswith("=IF(AND(ISNUMBER(") for c in gross)
    assert ws.freeze_panes == "E6"
    # every month column holds figures and every growth column holds formulas, with no overlap
    for c in range(5, 11):                       # last year's six months
        assert isinstance(gross[c - 1].value, (int, float)), head[c - 1]
    for c in range(12, 18):                      # the last five months and this one
        assert isinstance(gross[c - 1].value, (int, float)), head[c - 1]
    assert head[18] == "Sep'26 vs Aug'26" or "vs" in str(head[18])
    for c in range(19, 24):
        assert str(gross[c - 1].value).startswith("="), head[c - 1]
    mom_cols = [i for i, h in enumerate(head) if " vs " in str(h) and str(h).split(" vs ")[1].endswith("'26")]
    assert mom_cols and all(str(gross[i].value).startswith("=") for i in mom_cols)


# ------------------------------------------------------------- last known call

@need_fix
def test_last_known_call_stands_in_only_when_the_month_has_no_call(tmp_path_factory):
    def go(**kw):
        return run_month("Nykaa Fashion", MONTH, out_dir=tmp_path_factory.mktemp("o"),
                         store=Store(tmp_path_factory.mktemp("d")), **kw)
    codes = lambda r: {f.code for f in r.findings} | {f.code for f in r.estimate.findings}
    none = go()
    old = go(prior_call_path=str(CALL), prior_call_month="2026-06")
    both = go(transcript_path=str(CALL), prior_call_path=str(CALL), prior_call_month="2026-06")
    assert "PRIOR_CALL_USED" not in codes(none)
    assert "PRIOR_CALL_USED" in codes(old)
    assert "PRIOR_CALL_USED" not in codes(both), "this month's own call wins"
    assert old.weights.effective["expert_call"] > 0
    g = lambda r: r.estimate.value_for_par(r.reference, 241).value
    assert abs(g(old) / g(none) - 1) < 0.03, "an old call must not move the level"


def test_call_month_is_read_from_the_file_name():
    from fashion_intel.pipeline import infer_call_month
    assert infer_call_month("Myntra Aug 2026 call.docx") == "2026-08"
    assert infer_call_month("nykaa_call_jul'26.docx") == "2026-07"
    assert infer_call_month("call.docx") is None


# ------------------------------------------------------------- learning from past gaps

from fashion_intel import learning as L


def test_corrections_build_to_the_residual_and_stop_inside_the_cap():
    # Month 1: model 10% low on a category, nothing applied yet.
    m1 = [{"month": "2026-07", "categories": {"a": {"applied": 0.0, "resid": 0.10}}}]
    c1 = L.corrections(m1)["a"]
    assert c1 == pytest.approx(0.05)                       # half the residual
    # Month 2 used that, and is still 4% low: correction grows, not resets.
    m2 = m1 + [{"month": "2026-08", "categories": {"a": {"applied": c1, "resid": 0.04}}}]
    c2 = L.corrections(m2)["a"]
    assert c1 < c2 < 0.10
    # A huge persistent miss can never exceed the cap.
    big = [{"month": "m", "categories": {"a": {"applied": 0.10, "resid": 0.9}}}]
    assert L.corrections(big)["a"] == pytest.approx(L.CAP)
    # No residual left: the correction is simply kept.
    done = [{"month": "m", "categories": {"a": {"applied": 0.06, "resid": 0.0}}}]
    assert L.corrections(done)["a"] == pytest.approx(0.06)


def test_no_history_of_learning_means_no_correction():
    assert L.corrections([]) == {}
    assert L.corrections([{"month": "x", "history_gap": 0.1}]) == {}


def _fixed_mis(tmp):
    import openpyxl
    wb = openpyxl.load_workbook(MIS)
    ws = wb.active
    for r in range(1, ws.max_row + 1):
        if ws.cell(r, 2).value == "Tier 2+" and ws.cell(r, 1).value in (None, "Geographic Split"):
            ws.cell(r, 3).value = 0.10
    out = tmp / "Nykaa Fashion MIS clean.xlsx"
    wb.save(out)
    return out


@need_fix
def test_a_saved_month_teaches_the_next_month(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("d"))
    first = run_month("Nykaa Fashion", MONTH, mis_path=str(_fixed_mis(tmp_path_factory.mktemp("m"))), transcript_path=str(CALL),
                      out_dir=tmp_path_factory.mktemp("o"), store=store, save_history=True)
    assert first.learning and first.learning["categories"], "the month records category gaps"
    assert first.learning["n_mis"] >= 3 and first.learning["mis_cat_gap"] is not None
    from fashion_intel.history import calibration_records
    rec = calibration_records(store.root, "nykaa_fashion", "2026-10")
    assert rec and rec[-1].get("categories")
    nxt = run_month("Nykaa Fashion", "2026-10", out_dir=tmp_path_factory.mktemp("o"), store=store)
    assert nxt.estimate is not None
    g = lambda r: r.estimate.value_for_par(r.reference, 241).value
    assert g(nxt) > 0


# ------------------------------------------------------------- secondary data (news)

def _zara(tmp, news, weight):
    f = tmp.mktemp("s") / "Zara secondary.txt"
    f.write_text(news, encoding="utf-8")
    w = load_default_weights()
    w["weights"]["secondary"]["weight"] = weight
    w["weights"]["seasonality"]["weight"] -= weight
    return run_month("Zara", MONTH, out_dir=tmp.mktemp("o"), store=Store(tmp.mktemp("d")),
                     extra_sources={"secondary": str(f)} if news else {}, weights_cfg=w)


@need_ref
def test_secondary_news_moves_gross_by_direction_and_weight(tmp_path_factory):
    g = lambda r: r.estimate.value_for_par(r.reference, 241).value
    good = "Zara announces record growth and new store expansion in India.\nZara launches festive campaign."
    bad = "Zara faces probe and penalty over fraud complaint.\nZara announces layoffs and store closure."
    base = _zara(tmp_path_factory, "", 0.0)
    up, down = _zara(tmp_path_factory, good, 0.05), _zara(tmp_path_factory, bad, 0.05)
    assert g(down) < g(base) < g(up)
    assert abs(g(up) / g(base) - 1) <= 0.05 + 1e-9
    assert any(f.code == "SECONDARY_APPLIED" for f in up.estimate.findings)


@need_ref
def test_secondary_with_zero_weight_changes_nothing_and_says_so(tmp_path_factory):
    g = lambda r: r.estimate.value_for_par(r.reference, 241).value
    r0 = _zara(tmp_path_factory, "Zara announces record growth and expansion.", 0.0)
    base = _zara(tmp_path_factory, "", 0.0)
    assert g(r0) == pytest.approx(g(base))
    assert any(f.code == "SECONDARY_NO_WEIGHT" for f in r0.estimate.findings)


@need_ref
def test_news_about_another_player_is_ignored(tmp_path_factory):
    g = lambda r: r.estimate.value_for_par(r.reference, 241).value
    base = _zara(tmp_path_factory, "", 0.0)
    r = _zara(tmp_path_factory, "Myntra announces record growth and expansion everywhere.", 0.05)
    assert g(r) == pytest.approx(g(base))


# ------------------------------------------------------------- the brand-level weight

def test_a_brand_level_source_exists_and_the_weights_still_total_one():
    w = load_default_weights()
    assert "brand_level" in w["weights"] and w["weights"]["brand_level"]["label"] == "Brand-level data"
    assert sum(v["weight"] for v in w["weights"].values()) == pytest.approx(1.0)


def test_an_absent_zero_weight_source_is_not_reported_as_missing():
    w = load_default_weights()
    av = {k: True for k in w["weights"]}
    av.update(brand_level=False, logistics=False, traffic=False, social=False)
    r = resolve_weights(w, av)
    text = " ".join(r.warnings)
    assert "Brand-level data" not in text, "a source at 0% cannot be 'missing evidence'"
    assert "4 of 7" not in text and "3 of 7" in text


def test_a_brand_level_weight_takes_effect_when_the_file_is_supplied():
    w = load_default_weights()
    w["weights"]["brand_level"]["weight"] = 0.10
    w["weights"]["logistics"]["weight"] = 0.0
    av = {k: True for k in w["weights"]}
    r = resolve_weights(w, av)
    assert r.effective["brand_level"] == pytest.approx(0.10)
    av["brand_level"] = False
    r2 = resolve_weights(w, av)
    assert r2.effective["brand_level"] == 0.0
    assert sum(r2.effective.values()) == pytest.approx(1.0)



# ------------------------------------------------------------- one-sheet periods, detailed deviation, edited analysis

@need_ref
def test_the_analysis_sheet_carries_quarterly_and_half_yearly_beside_the_months(tmp_path_factory):
    import openpyxl
    r = run_month("Zara", MONTH, out_dir=tmp_path_factory.mktemp("o"), store=Store(tmp_path_factory.mktemp("d")))
    ws = openpyxl.load_workbook(r.outputs["analysis"])["Analysis"]
    head = [c.value for c in ws[5]]
    assert "Q2 FY27" in head and "H1 FY27" in head and "Q2 FY27 vs Q2 FY26" in head and "H1 FY27 vs H1 FY26" in head
    gross = next(row for row in ws.iter_rows(min_row=6) if row[0].value == 241)
    q = gross[head.index("Q2 FY27")].value
    cur = [i for i, h in enumerate(head) if h == "Sep'26"][-1]
    # the current quarter reads this sheet's own month cells, so an edit to a month flows into it
    assert q.startswith("=IF(COUNT(") and "History!" not in q and openpyxl.utils.get_column_letter(cur + 1) in q
    assert "History!" in gross[head.index("Q4 FY26")].value


@need_ref
def test_the_deviation_sheet_gives_every_category_levels_mom_and_yoy(tmp_path_factory):
    import openpyxl
    r = run_month("Zara", MONTH, out_dir=tmp_path_factory.mktemp("o"), store=Store(tmp_path_factory.mktemp("d")))
    ws = openpyxl.load_workbook(r.outputs["analysis"])["Deviation"]
    hdr = next(row for row in ws.iter_rows() if row[0].value == "Parameter")
    assert [c.value for c in hdr[3:15]][2:5] == ["MIS figure", "Our output", "Gap"]
    assert [c.value for c in hdr[9:15]] == ["MIS", "Ours", "Difference", "MIS", "Ours", "Difference"]
    top = [c.value for c in ws[hdr[0].row - 1] if c.value]
    assert any(str(x).startswith("MoM vs") for x in top) and any(str(x).startswith("YoY vs") for x in top)
    gross = next(row for row in ws.iter_rows(min_row=hdr[0].row + 1) if row[1].value == 241)
    assert gross[3].value and gross[4].value and gross[6].value, "last month, last year and our output are absolutes"
    assert str(gross[10].value).startswith("=IF(AND(ISNUMBER(G"), "our MoM is a formula on the stored month"
    cats = [row[0].value for row in ws.iter_rows(min_row=hdr[0].row + 1) if row[1].value is None and row[0].value]
    assert len(cats) > 5, "section and category rows group the metrics"


@need_ref
def test_an_edited_analysis_rebuilds_the_output_file_in_place(tmp_path_factory):
    import openpyxl
    from fashion_intel.export.from_analysis import rebuild_output
    od = tmp_path_factory.mktemp("o")
    r = run_month("Zara", MONTH, out_dir=od, store=Store(tmp_path_factory.mktemp("d")))
    before = Path(r.outputs["deliverable"])
    wb = openpyxl.load_workbook(r.outputs["analysis"])
    ws = wb["Analysis"]
    col = [i for i, c in enumerate(ws[5], 1) if c.value == "Sep'26"][-1]
    gross = next(row for row in ws.iter_rows(min_row=6) if row[0].value == 241)
    gross[col - 1].value = 4321.0
    edited = od / "edited.xlsx"
    wb.save(edited)
    out, info = rebuild_output(edited, od)
    assert out == before, "the Output file already there is the one replaced"
    assert info["edited"] == 1 and info["had_run_data"]
    o = openpyxl.load_workbook(out)
    assert o.sheetnames == ["Contents", "Myntra-WF", "Source", "Key"]
    up = next(row for row in o["Myntra-WF"].iter_rows(min_row=2) if row[2].value == 241)
    assert up[4].value == 4321.0
    src = next(row for row in o["Source"].iter_rows(min_row=5) if row[0].value == 241)
    assert src[3].value == "Assumed" and "changed" in src[6].value, "an edit is never shown as reported"
    other = next(row for row in o["Source"].iter_rows(min_row=5) if row[0].value == 242)
    assert other[3].value != "Assumed", "an untouched figure keeps its own source"

@need_ref
def test_the_analysis_flags_trend_changes_for_review(tmp_path_factory):
    import openpyxl
    r = run_month("Zara", MONTH, out_dir=tmp_path_factory.mktemp("o"), store=Store(tmp_path_factory.mktemp("d")))
    ws = openpyxl.load_workbook(r.outputs["analysis"])["Analysis"]
    head = [c.value for c in ws[5]]
    m, y = head.index("MoM trend") + 1, head.index("YoY trend") + 1
    assert y == m + 1 and str(ws.cell(3, 1).value).startswith('="Trend changes this month')
    gross = next(row for row in ws.iter_rows(min_row=6) if row[0].value == 241)
    f = gross[m - 1].value
    assert f.startswith("=IF(AND(ISNUMBER(") and '"Turned up"' in f and '"Slowing"' in f
    assert '"YoY turned negative"' in gross[y - 1].value
    assert ws.auto_filter.ref and ws.conditional_formatting
