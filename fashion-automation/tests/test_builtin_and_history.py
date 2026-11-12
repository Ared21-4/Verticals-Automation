"""Built-in reference, history, and building the whole output without a MIS or call."""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from fashion_intel.config_resolver import available_players
from fashion_intel.domain import Provenance
from fashion_intel.history import describe, load_overlay, remove_month
from fashion_intel.pipeline import commit_history, load_player_config, run_month
from fashion_intel.reference_store import default_reference
from fashion_intel.store import Store

ROOT = Path(__file__).resolve().parent.parent
PLAYERS = ["Myntra", "Ajio", "Zivame", "H&M", "M&S", "Clovia", "Bewakoof", "Limeroad",
           "Nykaa Fashion", "Shoppers Stop", "Zara", "Snapdeal", "Tata CLiQ"]
MONTH = "2026-09"

pytestmark = pytest.mark.skipif(default_reference() is None, reason="built-in reference missing")


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """Every player, no MIS, no call, no reference argument."""
    store = Store(tmp_path_factory.mktemp("d"))
    out = tmp_path_factory.mktemp("o")
    return {n: run_month(n, MONTH, out_dir=out, store=store) for n in PLAYERS}


def _sheet(res):
    return openpyxl.load_workbook(res.outputs["deliverable"])["Myntra-WF"]


def _vals(res):
    ws = _sheet(res)
    return {ws.cell(r, 3).value: ws.cell(r, 5).value for r in range(2, ws.max_row + 1)}


# ---------------------------------------------------------------- built-in reference

def test_exactly_the_thirteen_fashion_players_are_offered():
    rows = available_players(default_reference())
    assert sorted(r["name"] for r in rows) == sorted(PLAYERS)


def test_no_reference_argument_is_needed(runs):
    for n, r in runs.items():
        assert r.estimate is not None and not r.errors, f"{n}: {[f.code for f in r.errors]}"
        assert any(i["kind"] == "built-in reference" for i in r.manifest["inputs"])


def test_the_reference_is_not_copied_into_every_run(runs):
    r = runs["Zara"]
    assert not any("reference" in p.name for p in
                   (Path(r.outputs["deliverable"]).parent).glob("*reference*"))


# ----------------------------------------------- whole output with no MIS and no call

@pytest.mark.parametrize("name", PLAYERS)
def test_whole_topline_is_built_from_history_alone(name, runs):
    v = _vals(runs[name])
    for par, label in ((241, "gross GMV"), (243, "shipped GMV"), (247, "net GMV"),
                       (240, "AOV"), (468, "ASP")):
        assert isinstance(v.get(par), (int, float)), f"{name}: {label} (par {par}) is {v.get(par)!r}"
        assert v[par] > 0


@pytest.mark.parametrize("name", PLAYERS)
def test_apparel_gmv_is_built_for_every_player(name, runs):
    v = _vals(runs[name])
    n = lambda p: v[p] if isinstance(v.get(p), (int, float)) else 0.0
    for par in (457, 554):
        assert isinstance(v.get(par), (int, float)), f"{name}: par {par} is {v.get(par)!r}"
    # Levels add up over whatever the player itemises; absent branches count as nothing.
    if any(isinstance(v.get(p), (int, float)) for p in (1537, 1542, 1551)):    # nothing to add up if none itemised
        assert v[554] == pytest.approx(n(1537) + n(1542) + n(1551), abs=2e-3)
    assert v[457] == pytest.approx(v[554] + n(559) + n(562), abs=2e-3)
    assert v[457] <= v[243] * 1.001, "fashion GMV cannot exceed the platform total"


def test_partial_coverage_is_stated_not_hidden(runs):
    """A player that itemises only some categories must say its apparel figure is partial."""
    est = runs["Zivame"].estimate
    m = est.by_par[554].method
    assert "does not itemise" in m or "sum of the categories beneath it" in m
    hm = runs["H&M"].estimate.by_par[554]
    assert "does not itemise" in hm.method, hm.method


@pytest.mark.parametrize("name", PLAYERS)
def test_every_metric_the_players_sheet_supports_is_populated(name, runs):
    """With no MIS or call, nothing the player's own history can support is left null."""
    r = runs[name]
    v = _vals(r)
    ref = r.reference
    # A row can exist in a player's sheet with no data in it at all; that cannot be estimated.
    supported = [t.par_id for t in ref.template
                 if not t.added and t.par_id in ref.par_to_row
                 and _fresh(ref.series.get(ref.par_to_row[t.par_id], {}))]
    empty = [p for p in supported if v.get(p) == "null"]
    assert not empty, f"{name}: {len(empty)} metrics with history left unfilled: {empty[:8]}"


@pytest.mark.parametrize("name", PLAYERS)
def test_every_estimated_value_is_labelled(name, runs):
    est = runs[name].estimate
    for v in list(est.rows.values()) + list(est.by_par.values()):
        if v.is_null:
            continue
        assert v.provenance is not Provenance.REPORTED, "no MIS was supplied; nothing is reported"
        assert v.method


def test_stale_history_is_never_projected_forward(runs):
    """Shoppers Stop's app/session series end in 2024-25. They must stay null, not be invented."""
    v = _vals(runs["Shoppers Stop"])
    for par in (161, 2587, 2588, 2592, 2607):
        assert v.get(par) == "null", f"par {par} was projected from stale history: {v.get(par)}"
    src = openpyxl.load_workbook(runs["Shoppers Stop"].outputs["deliverable"])["Source"]
    why = {src.cell(r, 1).value: src.cell(r, 7).value for r in range(5, src.max_row + 1)}
    assert "too stale" in str(why[161])


def test_geography_and_splits_sum_to_one(runs):
    for n, r in runs.items():
        P = load_player_config(n)["platform_rows"]
        tot = sum(r.estimate.num(P[k]) for k in ("metro_pct", "tier1_pct", "tier2plus_pct")
                  if r.estimate.num(P[k]) is not None)
        assert tot == pytest.approx(1.0, abs=1e-9), n


def test_audit_summary_shows_apparel_gmv(runs):
    ws = openpyxl.load_workbook(runs["Ajio"].outputs["audit"])["Summary"]
    labels = [str(ws.cell(r, 1).value or "").strip() for r in range(1, ws.max_row + 1)]
    assert "Apparel GMV" in labels and "Fashion GMV" in labels


def test_output_format_is_the_same_for_every_player(runs):
    for n, r in runs.items():
        wb = openpyxl.load_workbook(r.outputs["deliverable"])
        assert wb.sheetnames == ["Contents", "Myntra-WF", "Source", "Key"], n
        ws = wb["Myntra-WF"]
        assert [ws.cell(1, c).value for c in range(1, 5)] == [
            "parent_question", "parameter_name", "par_id", "unit"]
        vals = [ws.cell(r_, 5).value for r_ in range(2, ws.max_row + 1)]
        first_null = next((i for i, x in enumerate(vals) if x == "null"), len(vals))
        assert all(x == "null" for x in vals[first_null:]), f"{n}: nulls are not all at the bottom"


# ------------------------------------------------------------------ history and new months

def test_a_new_month_runs_without_any_reference_upload(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("d"))
    r = run_month("Zara", "2026-10", out_dir=tmp_path_factory.mktemp("o"), store=store)
    assert r.estimate is not None and not r.errors
    assert any(f.code == "NEW_MONTH" for f in r.all_findings)


def test_skipping_a_month_is_refused(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("d"))
    r = run_month("Zara", "2026-11", out_dir=tmp_path_factory.mktemp("o"), store=store)
    assert r.estimate is None
    assert any(f.code == "REF_HISTORY_GAP" for f in r.errors)


def test_saved_history_feeds_the_next_month(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("d"))
    out = tmp_path_factory.mktemp("o")
    sep = run_month("Zara", "2026-09", out_dir=out, store=store, save_history=True)
    assert sep.manifest["history_saved"]["values"] > 20
    assert [m["month"] for m in describe(store.root, "zara")] == ["2026-09"]

    nov = run_month("Zara", "2026-11", out_dir=out, store=store)       # Oct unsaved: still refused
    assert nov.estimate is None
    octo = run_month("Zara", "2026-10", out_dir=out, store=store, save_history=True)
    assert octo.estimate is not None and not octo.errors
    nov = run_month("Zara", "2026-11", out_dir=out, store=store)       # Oct now saved
    assert nov.estimate is not None and not nov.errors
    assert nov.reference.has_month("2026-10")


def test_saved_month_replaces_the_stored_placeholder(tmp_path_factory):
    """After saving, the next run reads the saved Sep'26 rather than the workbook's own."""
    store = Store(tmp_path_factory.mktemp("d"))
    out = tmp_path_factory.mktemp("o")
    first = run_month("Ajio", "2026-09", out_dir=out, store=store)
    P = load_player_config("Ajio")["platform_rows"]
    before = first.reference.value(P["gross_gmv"], "2026-09")
    run_month("Ajio", "2026-09", out_dir=out, store=store, save_history=True)
    nxt = run_month("Ajio", "2026-10", out_dir=out, store=store)
    assert nxt.reference.value(P["gross_gmv"], "2026-09") == pytest.approx(
        first.estimate.num(P["gross_gmv"]))
    assert before != nxt.reference.value(P["gross_gmv"], "2026-09")


def test_saved_history_can_be_removed(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("d"))
    run_month("Zara", "2026-09", out_dir=tmp_path_factory.mktemp("o"), store=store,
              save_history=True)
    assert remove_month(store.root, "zara", "2026-09")
    assert load_overlay(store.root, "zara")["months"] == {}


def test_a_run_with_errors_is_not_saved(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("d"))
    r = run_month("Myntra", "2026-09", mis_path=str(ROOT / "Myntra MIS.xlsx"),
                  transcript_path=str(ROOT / "Myntra_September_Expert_Call_Insights.docx"),
                  out_dir=tmp_path_factory.mktemp("o"), store=store, save_history=True) \
        if (ROOT / "Myntra MIS.xlsx").exists() else None
    if r is None:
        pytest.skip("Myntra MIS not present")
    assert r.errors, "the supplied MIS has a known geographic-split error"
    assert "history_saved" not in r.manifest
    blocked = run_month("Zara", "2026-11", out_dir=tmp_path_factory.mktemp("o2"), store=store)
    assert blocked.estimate is None
    with pytest.raises(ValueError, match="Nothing to save"):
        commit_history(blocked, store)



def _fresh(series) -> bool:
    """Three or more points and an observation within the last six months."""
    pts = sorted(m for m in series if m < MONTH)
    return len(pts) >= 3 and pts[-1] >= "2026-03"

