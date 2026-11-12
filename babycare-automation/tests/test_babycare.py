"""Babycare portal tests: discovery, parameter binding, identities and a whole-month run."""

from __future__ import annotations

import json
from pathlib import Path

import openpyxl
import pytest

from babycare_intel.config_resolver import resolve_config
from babycare_intel.discovery import discover_players
from babycare_intel.domain import Severity
from babycare_intel.pipeline import run_month
from babycare_intel.readers.reference import load_reference
from babycare_intel.store import Store

ROOT = Path(__file__).resolve().parent.parent
REF = str(ROOT / "reference" / "Babycare Reference.xlsx")
NEXT = "2026-10"

# GMV, ASP, share, units for Kids Fashion, Diaper & Nappies, Supplies, Gear, Others
CATS = [(293, 3140, 7711, 7721), (294, 515, 7712, 7722), (295, 516, 7713, 7723),
        (296, 522, 7714, 7724), (7701, 523, 7715, 7725)]

pytestmark = pytest.mark.skipif(not Path(REF).exists(), reason="reference workbook not present")


@pytest.fixture(scope="module")
def players():
    return discover_players(REF)


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("run")
    return run_month("Firstcry", NEXT, reference_path=REF, out_dir=tmp / "out",
                     store=Store(tmp / "data"))


def _template():
    return json.loads((ROOT / "babycare_intel" / "config" / "babycare_template.json")
                      .read_text(encoding="utf-8"))["rows"]


def _out_values(result):
    ws = openpyxl.load_workbook(result.outputs["deliverable"], data_only=True)["Babycare-WF"]
    return {int(r[2]): r[4] for r in ws.iter_rows(min_row=2, values_only=True) if r[2] is not None}


# ---------------------------------------------------------------- discovery and binding

def test_firstcry_is_found_and_usable(players):
    assert [p.name for p in players] == ["Firstcry"]
    assert players[0].usable
    assert players[0].months[0] == "2021-01"


def test_category_pars_bind_to_their_own_blocks(players):
    """The five category labels repeat in four blocks; every par must land on its own row."""
    pars = players[0].par_ids
    for gmv, asp, share, units in CATS:
        rows = [pars[p] for p in (gmv, asp, share, units)]
        assert len(set(rows)) == 4, (gmv, rows)
    assert pars[7711] == pars[293] + 34          # share block sits 34 rows under the GMV block


def test_platform_pars_bind(players):
    pars = players[0].par_ids
    for p in (238, 240, 241, 242, 243, 245, 246, 247, 7301, 7700, 7733, 286, 287, 476, 477, 478):
        assert p in pars, p


def test_reference_loads_with_the_template():
    cfg = resolve_config("Firstcry", reference_path=REF)
    ref = load_reference(REF, cfg)
    assert len(_template()) == 86
    assert len(ref.template) == 86 + 4              # plus shipped/net GMV, returns and ASP (Additional metrics)
    assert ref.value(ref.par_to_row[241], "2026-09") > 0


# ---------------------------------------------------------------- a whole month

def test_run_has_no_errors(result):
    assert not result.errors


def test_output_follows_the_template_in_order(result):
    ws = openpyxl.load_workbook(result.outputs["deliverable"], data_only=True)["Babycare-WF"]
    got = [(r[0], r[1], r[2], r[3]) for r in ws.iter_rows(min_row=2, values_only=True) if r[2] is not None]
    want = [tuple(r) for r in _template()]
    assert [g[2] for g in got][:len(want)] == [w[2] for w in want]        # the upload rows, in order
    assert [g[2] for g in got][len(want):] == [243, 247, 7301, 468]        # then the Additional metrics


def test_platform_identities(result):
    v = _out_values(result)
    assert v[242] == pytest.approx(v[7803])                          # cancellation carried to Babycare
    assert v[286] + v[287] == pytest.approx(1)
    assert v[476] + v[477] + v[478] == pytest.approx(1, abs=2e-4)     # the upload sheet rounds each share to 4 dp
    assert v[7806] + v[7807] == pytest.approx(1)
    assert v[7808] + v[7809] + v[7810] == pytest.approx(1, abs=2e-4)


def test_babycare_block_is_filled_and_consistent(result):
    v = _out_values(result)
    for par in range(7801, 7813):
        assert isinstance(v[par], (int, float)), par
    assert v[7804] == pytest.approx(v[294] + v[295] + v[296] + _gmv_others(result))
    assert 0 < v[7802] < v[7801]                                      # users fewer than transactions
    assert v[7805] == pytest.approx(v[7804] * 1e4 / _babycare_units(result), rel=1e-6)


def test_fashion_block_is_filled(result):
    v = _out_values(result)
    for par in (4254, 3616, 4374, 457, 1551, 473, 514, 4491, 4503, 4515, 4527, 4539, 4551, 4563):
        assert isinstance(v[par], (int, float)), par
    assert v[457] == v[1551] and v[473] == v[514]


def test_bpc_and_home_rows_are_null_not_zero(result):
    v = _out_values(result)
    for par in (250, 580, 425, 458, 4255, 4257, 3612, 4371):
        assert v[par] in (None, "null"), (par, v[par])


def test_a_month_with_no_column_is_refused(tmp_path):
    """Validation stops the run rather than guessing a month the sheet has no column for."""
    res = run_month("Firstcry", "2030-01", reference_path=REF, out_dir=tmp_path / "out",
                    store=Store(tmp_path / "data"))
    assert res.errors and not res.outputs


# ---------------------------------------------------------------- helpers

def _row_value(result, par):
    est = result.estimate
    v = est.by_par.get(par)
    return None if v is None else v.value


def _gmv_others(result):
    return _row_value(result, 7701)


def _babycare_units(result):
    return sum(_row_value(result, p) for p in (7722, 7723, 7724, 7725))


def test_analysis_has_units_and_old_files_can_be_upgraded(tmp_path, monkeypatch):
    from babycare_intel.export import workbook as xl
    from babycare_intel.export.from_analysis import add_units_to_analysis

    def analysis(sub):
        run_month("Firstcry", NEXT, reference_path=REF, out_dir=tmp_path / sub / "out",
                  store=Store(tmp_path / sub / "data"))
        return next((tmp_path / sub / "out").rglob("*Analysis.xlsx"))

    fresh = openpyxl.load_workbook(analysis("new"))["Analysis"]
    pars = {c.value for c in fresh["A"] if isinstance(c.value, int)}
    assert {7720, 7721, 7722, 7723, 7724, 7725} <= pars
    with monkeypatch.context() as m:                                  # a file made before the units rows existed
        m.setattr(xl, "_unit_rows", lambda cfg, present: [])
        old = analysis("old")
    out = tmp_path / "upgraded.xlsx"
    assert add_units_to_analysis(old, out)["added"] >= 6
    ws = openpyxl.load_workbook(out)["Analysis"]
    assert {7720, 7721, 7722, 7723, 7724, 7725} <= {c.value for c in ws["A"] if isinstance(c.value, int)}
    assert {7711, 7712, 7713, 7714, 7715, 293, 7701} <= {c.value for c in ws["A"] if isinstance(c.value, int)}   # the shares
    with pytest.raises(ValueError):                                   # a second pass has nothing to add
        add_units_to_analysis(out, tmp_path / "again.xlsx")
