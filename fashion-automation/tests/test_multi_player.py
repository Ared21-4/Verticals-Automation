"""Multi-player tests: discovery, config resolution and running a non-Myntra player.

Myntra is the only player with a tuned configuration. These tests cover the
shared path every other player takes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fashion_intel.config_resolver import (available_players, player_key,
                                           resolve_config)
from fashion_intel.discovery import discover_players
from fashion_intel.domain import VIRTUAL_ROW_BASE, Provenance, is_sheet_row
from fashion_intel.pipeline import load_player_config, run_month
from fashion_intel.store import Store

ROOT = Path(__file__).resolve().parent.parent
REF = ROOT / "reference" / "Fashion Reference.xlsx"
MONTH = "2026-09"

pytestmark = pytest.mark.skipif(not REF.exists(), reason="reference workbook not present")

# Players with data through the reporting month, none of which has a tuned config.
OTHERS = ["H&M", "Zara", "Shoppers Stop", "Ajio", "Bewakoof", "Nykaa Fashion",
          "M&S", "Clovia", "Limeroad", "Zivame"]


@pytest.fixture(scope="module")
def players():
    return discover_players(str(REF))


# ---------------------------------------------------------------- discovery

def test_discovers_the_workbooks_players(players):
    names = {p.name for p in players}
    for expected in ("Myntra", "H&M", "Zara", "Ajio", "Shoppers Stop", "Nykaa Fashion", "M&S"):
        assert expected in names, f"{expected} not discovered"
    for expected in ("Snapdeal", "Tata CLiQ"):
        assert expected in names, f"{expected} not discovered"
    assert len(players) == 13, [p.name for p in players]


def test_versioned_sheets_collapse_to_one_player(players):
    names = [p.name for p in players]
    assert len(names) == len(set(names)), "a player appears twice"
    # The newest sheet wins, not the _OLD one.
    myntra = next(p for p in players if p.name == "Myntra")
    assert myntra.sheet == "Myntra_AJ"
    limeroad = next(p for p in players if p.name == "Limeroad")
    assert limeroad.sheet == "Limeroad revised"


def test_all_thirteen_fashion_players_are_runnable(players):
    assert all(p.usable for p in players), [p.name for p in players if not p.usable]
    assert {p.name for p in players} == {"Myntra", "Ajio", "Zivame", "H&M", "M&S", "Clovia",
                                         "Bewakoof", "Limeroad", "Nykaa Fashion",
                                         "Shoppers Stop", "Zara", "Snapdeal", "Tata CLiQ"}


def test_every_player_sheet_shares_the_same_shape(players):
    """par_id in column A. Most sheets put it on row 5; Snapdeal and Tata CLiQ on row 1, with the
    months straight after the name. The month axis start varies and is detected."""
    for p in players:
        own_workbook = p.name in ("Snapdeal", "Tata CLiQ")
        assert p.header_row == (1 if own_workbook else 5), p.sheet
        assert p.first_month_col >= (3 if own_workbook else 4), p.sheet
        assert 241 in p.par_ids or not p.usable


def test_month_axis_start_is_detected_not_assumed(players):
    """Nykaa's sheets begin their months later than everyone else's."""
    starts = {p.sheet: p.first_month_col for p in players}
    assert starts["Nykaa Fashion"] == 6
    assert starts["Myntra_AJ"] == 4
    # Whatever the start column, the months still parse.
    for p in players:
        if p.usable:
            assert p.months, f"{p.sheet} found no months"


def test_month_headers_annotated_with_sale_names_are_not_lost(players):
    """AJIO labels months like "Dec'22 (Big Bold Sale)"; those are still months."""
    ajio = next(p for p in players if p.name == "Ajio")
    assert len(ajio.months) >= 55, (
        f"AJIO kept only {len(ajio.months)} months; annotated headers are being dropped")
    assert "2022-12" in ajio.months
    assert "2024-03" in ajio.months


# ------------------------------------------------------- config resolution

def test_player_without_a_config_file_still_resolves():
    cfg = resolve_config("H&M", reference_path=str(REF))
    assert cfg["reference"]["model_sheet"] == "H&M"
    assert cfg["platform_rows"], "no metrics bound"
    assert cfg["profile"] == "fashion"
    assert not cfg.get("l2_categories"), "H&M should have no category tree"


def test_myntra_keeps_its_tuned_configuration():
    cfg = resolve_config("Myntra", reference_path=str(REF))
    assert cfg["reference"]["model_sheet"] == "Myntra_AJ"
    assert cfg["l2_categories"] and cfg["mis_taxonomy_map"]
    assert cfg["corroboration"]
    # Explicit row pins survive the merge.
    assert cfg["platform_rows"]["shipped_gmv"] == 8


def test_par_ids_bind_to_each_players_own_rows():
    hm = resolve_config("H&M", reference_path=str(REF))["platform_rows"]
    zara = resolve_config("Zara", reference_path=str(REF))["platform_rows"]
    assert hm["gross_gmv"] != zara["gross_gmv"], "rows must be per-player"
    assert is_sheet_row(hm["gross_gmv"]) and is_sheet_row(zara["gross_gmv"])


def test_absent_metrics_get_a_virtual_slot_not_a_sheet_row():
    cfg = resolve_config("H&M", reference_path=str(REF))
    rows = cfg["platform_rows"]
    assert not is_sheet_row(rows["shipped_gmv"]), "H&M has no par 243"
    assert rows["shipped_gmv"] >= VIRTUAL_ROW_BASE
    assert "shipped_gmv (par 243)" in cfg["unresolved_metrics"]


def test_player_key_is_filesystem_safe():
    assert player_key("Marks & Spencer") == "marks_spencer"
    assert player_key("Bewakoof.com") == "bewakoof_com"
    assert player_key("H&M") == "h_m"


def test_available_players_lists_configured_and_discovered():
    rows = available_players(str(REF))
    by = {r["name"]: r for r in rows}
    assert by["Myntra"]["configured"] is True
    assert by["H&M"]["configured"] is False
    assert by["H&M"]["usable"] is True


# ------------------------------------------------------------- actual runs

@pytest.mark.parametrize("name", OTHERS)
def test_other_players_run_end_to_end(name, tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("d"))
    r = run_month(name, MONTH, str(REF), out_dir=tmp_path_factory.mktemp("o"), store=store)
    assert r.estimate is not None, f"{name} produced no estimate"
    assert not r.errors, f"{name} raised errors: {[f.code for f in r.errors]}"
    assert all(Path(p).exists() for p in r.outputs.values())

    cfg = load_player_config(name, str(REF))
    P = cfg["platform_rows"]
    gross = r.estimate.num(P["gross_gmv"])
    assert gross is not None and gross > 0, f"{name} has no gross GMV"


@pytest.mark.parametrize("name", ["H&M", "Ajio", "Zara"])
def test_gross_anchored_players_derive_shipped_gmv(name, tmp_path_factory):
    """These sheets store gross GMV only; shipped must follow from cancellation."""
    store = Store(tmp_path_factory.mktemp("d"))
    r = run_month(name, MONTH, str(REF), out_dir=tmp_path_factory.mktemp("o"), store=store)
    cfg = load_player_config(name, str(REF))
    P, e = cfg["platform_rows"], r.estimate
    gross, canc, shipped = e.num(P["gross_gmv"]), e.num(P["cancellation_pct"]), e.num(P["shipped_gmv"])
    assert None not in (gross, canc, shipped)
    assert shipped == pytest.approx(gross * (1 - canc), rel=1e-9)
    assert e.rows[P["shipped_gmv"]].provenance is Provenance.DERIVED


@pytest.mark.parametrize("name", OTHERS)
def test_geography_always_sums_to_one(name, tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("d"))
    r = run_month(name, MONTH, str(REF), out_dir=tmp_path_factory.mktemp("o"), store=store)
    cfg = load_player_config(name, str(REF))
    P, e = cfg["platform_rows"], r.estimate
    total = sum(v for k in ("metro_pct", "tier1_pct", "tier2plus_pct")
                if (v := e.num(P[k])) is not None)
    assert total == pytest.approx(1.0, abs=1e-9)


def test_players_without_a_taxonomy_say_so_rather_than_inventing(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("d"))
    r = run_month("H&M", MONTH, str(REF), out_dir=tmp_path_factory.mktemp("o"), store=store)
    assert any(f.code == "NO_CATEGORY_TAXONOMY" for f in r.all_findings)
    assert not r.estimate.l2


def test_each_player_writes_its_own_named_folder(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("d"))
    out = tmp_path_factory.mktemp("o")
    for name in ("H&M", "Zara"):
        r = run_month(name, MONTH, str(REF), out_dir=out, store=store)
        folder = Path(r.outputs["deliverable"]).parent.name
        assert folder.startswith(f"{name} Sep 2026") or folder.startswith("H&M Sep 2026")
    assert len(list(Path(out).iterdir())) == 2


def test_runs_for_different_players_are_kept_apart(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("d"))
    out = tmp_path_factory.mktemp("o")
    run_month("H&M", MONTH, str(REF), out_dir=out, store=store)
    run_month("Zara", MONTH, str(REF), out_dir=out, store=store)
    assert len(store.list_runs("H&M")) == 1
    assert len(store.list_runs("Zara")) == 1
    assert len(store.list_runs()) == 2
