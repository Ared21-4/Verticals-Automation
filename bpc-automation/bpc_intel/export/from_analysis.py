"""Rebuild a month's Output workbook from an Analysis workbook that has been edited.

The Analysis workbook's current-month column is the working copy: change a figure there, upload the
file, and this writes the Output workbook (Contents, upload sheet, Source, Key) from it, replacing
the Output file already saved for that player and month. Nothing is re-estimated.

Provenance stays honest: a figure that still matches what the run produced keeps its source; one
that was changed or filled in is recorded as an analyst figure (assumed), never as reported.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import openpyxl

from ..config_resolver import player_key
from ..domain import Confidence, Provenance, Value, is_sheet_row
from ..history import load_overlay, save_month
from ..engine.estimate import MonthEstimate
from ..pipeline import _pretty_month, load_player_config
from ..readers.reference import load_reference
from ..reference_store import resolve_reference
from . import workbook as xl
from .collate import read_analysis

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_LABEL_PROV = {v[0]: k for k, v in xl.DOT.items()}          # "Reported" -> Provenance.REPORTED
SOURCE_HEAD = "This month's source"


def _month_key(label: str) -> str:
    m = re.match(r"\s*([A-Za-z]{3})[a-z]*\s*'?\s*(\d{2})\s*$", label)
    if not m or m.group(1).lower() not in _MONTHS:
        raise ValueError(f"cannot read the month '{label}' from the Analysis title")
    return f"20{m.group(2)}-{_MONTHS[m.group(1).lower()]:02d}"


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def _existing_dir(root: Path, base: str) -> Path:
    """The folder already holding this month's output (the newest if repeat runs left several)."""
    found = [p for p in root.glob(f"{base}*") if p.is_dir() and (p / f"{base} - Output.xlsx").exists()]
    return max(found, key=lambda p: (p / f"{base} - Output.xlsx").stat().st_mtime) if found else root / base


# Headline figures whose edit size is reported as the "edited vs model" gap.
_HEADLINE = (241, 243, 240, 242, 238)


def _learn(cfg: dict, ref, month: str, est: MonthEstimate, orig: dict[int, dict], pa,
           data_root: str | Path) -> dict[str, Any]:
    """Keep the analyst's edited month as history and let the model learn from how it differs.

    1. The edited figures replace that month in saved history (basis 'analyst_edited'), so later
       months build on them, not on the model's own estimate.
    2. Category shares the analyst changed become a residual against the run's share. It goes in
       the month's learning record under src 'analyst', which is what `learning.corrections` reads:
       half the residual is applied to the next month's mix, capped at +/-10% in total.
    3. The mean size of the edits to the headline figures is stored as 'edited vs model'.
    """
    import datetime as _dt
    key = player_key(cfg["player"])
    for par, v in est.by_par.items():                  # history is stored by sheet row
        row = ref.par_to_row.get(par)
        if row and is_sheet_row(row) and v is not None and not v.is_null:
            est.rows[row] = v

    block = load_overlay(data_root, key).get("months", {}).get(month, {})
    cal = dict(block.get("calibration") or {})
    cats = dict(cal.get("categories") or {})
    learned = 0
    def pair(par):
        o = orig.get(par)
        return (o["value"] if o else None), (pa.rows.get(par) or {}).get("cur")

    leaves = (cfg.get("category_tree") or {}).get("leaves", [])
    gm = {lf["gmv"]: pair(lf["gmv"]) for lf in leaves if lf.get("gmv")}
    gm = {g: (o, c) for g, (o, c) in gm.items() if o and c is not None}
    o_s, c_s = pair(243)
    if o_s and c_s is not None:                            # shares of shipped GMV, where it is in the workbook
        den_o, den_c = o_s, c_s
    else:                                                  # else each category's part of those in the workbook
        den_o, den_c = sum(o for o, _ in gm.values()), sum(c for _, c in gm.values())
    for lf in leaves:
        sp = lf.get("share")
        if sp is None:
            continue
        o_sh, c_sh = pair(sp)
        r_share = c_sh / o_sh if o_sh and c_sh is not None else None    # the share row, if present
        r_gmv = None                                                     # the share implied by the GMV row
        if lf.get("gmv") in gm and den_o and den_c:
            o_g, c_g = gm[lf["gmv"]]
            r_gmv = (c_g / den_c) / (o_g / den_o)
        # Edit either row: take the one the analyst actually moved (a share row can sit unchanged
        # beside an edited GMV).
        ratio = r_share if r_share is not None and abs(r_share - 1) > 1e-9 else (
            r_gmv if r_gmv is not None else r_share)
        if ratio is None:
            continue
        k = f"par{sp}"
        cats[k] = {"applied": (cats.get(k) or {}).get("applied", 0.0),
                   "resid": round(ratio - 1, 5), "src": "analyst"}
        learned += abs(cats[k]["resid"]) > 1e-9
    gaps = []
    for par in _HEADLINE:
        o, cur = orig.get(par), (pa.rows.get(par) or {}).get("cur")
        if (pa.rows.get(par) or {}).get("cur_formula"):
            continue
        if o and o["value"] and cur is not None:
            gaps.append(abs(cur / o["value"] - 1))
    if cats:
        cal["categories"] = cats
    if gaps:
        cal["analyst_gap"] = round(sum(gaps) / len(gaps), 5)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    saved = save_month(data_root, key, month, est, ref, f"analyst-edit-{stamp}", False, cal,
                       basis="analyst_edited")
    return {"saved_values": saved["values"], "categories_learned": int(learned),
            "headline_gap": cal.get("analyst_gap")}


def _link_only(wb, wv, ws, wsv, pa, cfg, ref, month, arow, cur_c, path_out) -> dict[str, Any]:
    """An Analysis that already has the units rows: turn its unedited plain-number derived cells into formulas."""
    from openpyxl.utils import get_column_letter as L

    curv = {}
    for p, r in arow.items():
        v = wsv.cell(r, cur_c).value
        curv[p] = v if v is not None else (pa.rows.get(p) or {}).get("cur")
    n = xl._link_formulas(ws, arow, L(cur_c), curv, cfg, month, ref, keep_formulas=True)
    if not n:
        raise ValueError("this Analysis already has the units rows and its formulas, so there is nothing to add")
    wb.save(path_out)
    return {"added": 0, "linked": n, "path": str(path_out), "player": cfg["display_name"], "month": month}


def add_units_to_analysis(path_in: str | Path, path_out: str | Path) -> dict[str, Any]:
    """Add the units-by-category rows to an Analysis workbook made before they existed.

    The rows go below the last row, so nothing already in the file moves and none of its edits, formulas or
    overrides are touched. This month's units are formulas on the file's own GMV and ASP cells (so an edit
    there flows through); earlier months come from the reference workbook. The original is never changed:
    the upgraded copy is written to `path_out`. Threaded comments do not survive a save outside Excel."""
    from openpyxl.utils import get_column_letter as L

    pa = read_analysis(path_in)
    month = _month_key(pa.month)
    cfg = load_player_config(pa.player)
    ref = load_reference(resolve_reference(None), cfg)
    wb, wv = openpyxl.load_workbook(path_in), openpyxl.load_workbook(path_in, data_only=True)
    ws, wsv = wb[pa.analysis_sheet], wv[pa.analysis_sheet]

    head = [c.value for c in ws[5]]
    label = xl.month_label(month)
    if label not in head:
        raise ValueError(f"cannot find the {label} column in the Analysis sheet")
    n = 6
    cur_c = head.index(label) + 1
    c_rec = cur_c - n + 1
    lay = {"c_yag": c_rec - n - 1, "c_rec": c_rec, "c_mom": c_rec + n + 1, "c_yoy": c_rec + n + 1 + n,
           "n": n, "width": len(head)}
    lay["c_avg"] = lay["c_yoy"] + n + 1
    if lay["c_yag"] < 5 or " vs " not in str(head[lay["c_mom"] - 1] or ""):
        raise ValueError("this Analysis sheet does not have the standard column layout")

    arow = {c.value: c.row for c in ws["A"][5:] if isinstance(c.value, int)}
    todo = xl._unit_rows(ref, cfg, set(arow))
    if not todo:                                                # nothing to add: still link its derived cells
        return _link_only(wb, wv, ws, wsv, pa, cfg, ref, month, arow, cur_c, path_out)
    col = L(cur_c)

    def num(par):                                               # the cell's figure, else the run's (no stored result)
        v = wsv.cell(arow[par], cur_c).value if par in arow else None
        return v if v is not None else (pa.rows.get(par) or {}).get("cur")

    scale = 1.0
    tot = None
    fc: dict = {}
    tree: dict = {}
    rec = [xl.shift_months(month, -i) for i in range(n - 1, -1, -1)]
    yag = [xl.shift_months(m, -12) for m in rec]
    prior = xl.shift_months(month, -1)
    wanted = {t.par_id for t, _s in todo}

    parts: dict[int, tuple[str, float]] = {}                    # units par -> (this month's formula, its figure)
    by_seg: dict[Any, list[int]] = {}
    for lf in (cfg.get("l2_categories") or {}).values():
        g_, a_, u_ = (ref.row_to_par.get(lf.get(k)) for k in ("gmv", "asp", "units"))
        gv, av = num(g_), num(a_)
        if u_ in wanted and g_ in arow and a_ in arow and isinstance(gv, (int, float)) and isinstance(av, (int, float)) and av:
            parts[u_] = (f"={col}{arow[g_]}*10/{col}{arow[a_]}", gv * 10 / av)
            by_seg.setdefault(lf.get("segment"), []).append(u_)
    totals: dict[int, tuple[str, float]] = {}
    for sk, sv in (cfg.get("segments") or {}).items():
        tp, cats = ref.row_to_par.get(sv.get("items")), by_seg.get(sk) or []
        if tp not in wanted or not cats:
            continue
        was = ref.value(sv.get("items"), prior)                 # the segment total must be the sum of its categories
        was_sum = sum(ref.value_by_par(p, prior) or 0.0 for p in cats)
        if was and was_sum and abs(was_sum / was - 1) <= 0.02:
            totals[tp] = ("", sum(parts[p][1] for p in cats))
            by_seg[("tot", tp)] = cats
    lvals: dict[int, float] = {}
    fx: dict[int, Any] = {}
    ul = xl.unit_layout(ref)
    S_ = (cfg.get("metrics") or {}).get("shipped_gmv")
    if ul and S_ in arow:
        lvals = {p: x for p, x in xl.layout_values(ul, num, num(S_)).items() if p in wanted}
        lc = lambda p: p in lvals
        for e in ul["entries"]:
            if e["leaf"] and lc(e["par"]) and e["share"] in arow and e["asp"] in arow:
                fx[e["par"]] = (lambda e=e: f"={col}{arow[e['share']]}*{col}{arow[S_]}*10/{col}{arow[e['asp']]}")
            elif not e["leaf"] and lc(e["par"]):
                fx[e["par"]] = (lambda e=e: "=" + "+".join(f"{col}{arow[p]}" for p in e["kids"] if p in lvals))
        fx[ul["total"]["par"]] = lambda: "=" + "+".join(f"{col}{arow[e['par']]}" for e in ul["entries"]
                                                          if e["top"] and e["par"] in lvals)
        for e in ul["entries"]:
            if e["ipar"] in lvals:
                fx[e["ipar"]] = (lambda e=e: f"={col}{arow[e['par']]}/{col}{arow[ul['total']['par']]}")
    r = ws.max_row + 1
    rel_rngs: list[str] = []
    pp_rngs: list[str] = []
    sno = max([c.value for c in ws["B"][5:] if isinstance(c.value, int)] or [0])
    for i in range(1, lay["width"] + 1):
        ws.cell(r, i).fill = xl.FILL_GROUP
    ws.cell(r, 3, "Units by category").font = xl.GROUP
    r += 1
    q = None
    figures: dict[int, float] = {}
    for t, _sec in todo:
        cur = (parts.get(t.par_id) or totals.get(t.par_id) or (None, lvals.get(t.par_id)))[1]
        if q != t.parent_question:
            q = t.parent_question
            ws.cell(r, 3, q).font = xl.TD_B
            r += 1
        sno += 1
        arow[t.par_id] = r
        figures[t.par_id] = cur
        row = ref.par_to_row.get(t.par_id)
        dh = xl.derived_history(ref, cfg).get(t.par_id, {})
        hist = [xl._hist_val(ref, row, dh, m) for m in rec[:-1]]
        yhist = [xl._hist_val(ref, row, dh, m) for m in yag]
        xl._analysis_row(ws, r, sno, t, hist, yhist, cur, None, lay, rel_rngs, pp_rngs)
        ws.cell(r, lay["c_avg"] + 4, "Derived").font = xl.Font(name="Calibri", size=10, color=xl.DOT[Provenance.DERIVED][1])
        r += 1
    for t, _sec in todo:                                        # this month's cells become formulas
        cell = ws[f"{col}{arow[t.par_id]}"]
        if t.par_id in parts:
            cell.value = parts[t.par_id][0]
        elif t.par_id in fx:
            cell.value = fx[t.par_id]()
        elif t.par_id in totals:
            cell.value = "=" + "+".join(f"{col}{arow[p]}" for p in by_seg[("tot", t.par_id)])
    if xl.RUN_DATA in wb.sheetnames:                            # so a later edit is measured against these
        rd = wb[xl.RUN_DATA]
        k = rd.max_row + 1
        for t, _s in todo:
            if figures.get(t.par_id) is not None:
                for j, x in enumerate([t.par_id, figures[t.par_id], Provenance.DERIVED.value, "medium",
                                       "Units = GMV x 10^7 / ASP, added to an earlier Analysis file", month], 1):
                    rd.cell(k, j, x)
                k += 1
    # link the old file's other derived cells too, but only unedited plain numbers that equal the formula's result
    curv = {p: num(p) for p in arow}
    curv.update({p: v for p, v in figures.items() if v is not None})
    linked_n = xl._link_formulas(ws, arow, col, curv, cfg, month, ref, keep_formulas=True)
    xl.add_growth_colours(ws, rel_rngs, pp_rngs)
    wb.save(path_out)
    return {"added": len(todo), "linked": linked_n, "path": str(path_out), "player": cfg["display_name"], "month": month}


def rebuild_output(analysis_path: str | Path, out_root: str | Path = "outputs",
                   data_root: str | Path | None = None) -> tuple[Path, dict[str, Any]]:
    """Write the Output workbook from an edited Analysis workbook. Returns (path, summary).

    With `data_root` the edited month is also kept as history and learned from (see `_learn`)."""
    pa = read_analysis(analysis_path)                        # ValueError if it is not an Analysis workbook
    month = _month_key(pa.month)
    wb = openpyxl.load_workbook(analysis_path, data_only=False)
    orig: dict[int, dict] = {}
    if xl.RUN_DATA in wb.sheetnames:
        for row in wb[xl.RUN_DATA].iter_rows(min_row=2, values_only=True):
            if isinstance(row[0], int):
                orig[row[0]] = {"value": row[1], "prov": row[2], "conf": row[3], "how": row[4]}
    # an older file has no run data: keep the source its own column names, and edits cannot be told
    shown: dict[int, str] = {}
    ws = wb[pa.analysis_sheet]
    head = [c.value for c in ws[5]]
    if not orig and SOURCE_HEAD in head:
        col = head.index(SOURCE_HEAD)
        shown = {row[0].value: row[col].value for row in ws.iter_rows(min_row=6)
                 if isinstance(row[0].value, int)}

    cfg = load_player_config(pa.player)
    ref = load_reference(resolve_reference(None), cfg)
    est = MonthEstimate(player=cfg["player"], month=month)
    edited = filled = cleared = recalculated = uncached = 0
    for par, d in pa.rows.items():
        cur, o = d["cur"], orig.get(par)
        if cur is None and d.get("cur_formula") and o is not None and o["value"] is not None:
            cur, uncached = o["value"], uncached + 1         # a formula with no stored result: keep the run's figure
        if cur is None:
            cleared += o is not None and o["value"] is not None
            continue
        if o is not None and _same(cur, o["value"]):
            est.set_par(par, Value(cur, Provenance(o["prov"]), d["unit"], source="Analysis workbook",
                                   method=o["how"] or "", confidence=Confidence(o["conf"] or "medium")))
        elif d.get("cur_formula") and o is not None:         # a formula that moved because an input was edited
            recalculated += 1
            was = f" The run had {o['value']:.6g}." if o["value"] is not None else ""
            est.set_par(par, Value(cur, Provenance.DERIVED, d["unit"], source="Analysis workbook",
                                   method="Recalculated in the Analysis workbook from the figures edited "
                                          f"there.{was}", confidence=Confidence.MEDIUM))
        elif o is None and not orig:
            prov = _LABEL_PROV.get(shown.get(par) or "", Provenance.ASSUMED)
            est.set_par(par, Value(cur, prov, d["unit"], source="Analysis workbook",
                                   method="Taken from the uploaded Analysis workbook."))
        else:
            if o is not None:
                edited += 1
                was = f" The run had {o['value']:.6g}." if o["value"] is not None else ""
                why = "changed"
            else:
                filled += 1
                was, why = "", "entered"
            est.set_par(par, Value(cur, Provenance.ASSUMED, d["unit"], source="Analysis workbook",
                                   method=f"Analyst figure, {why} in the Analysis workbook.{was}",
                                   confidence=Confidence.LOW))

    base = f"{cfg['display_name']} {_pretty_month(month)}"
    folder = _existing_dir(Path(out_root), base)
    ctx = {"config": cfg, "reference": ref, "estimate": est, "month": month}
    path = xl.write_output_workbook(folder / f"{base} - Output.xlsx", ctx)
    info = {"player": cfg["display_name"], "month": month, "values": len(est.by_par),
            "edited": edited, "filled": filled, "cleared": int(cleared), "had_run_data": bool(orig),
            "recalculated": recalculated, "uncached": uncached}
    if data_root is not None:
        info["learned"] = _learn(cfg, ref, month, est, orig, pa, data_root)
    return path, info
