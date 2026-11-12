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
    todo = xl._unit_rows(cfg, set(arow))
    if not todo:                                                # nothing to add: still link its derived cells
        return _link_only(wb, wv, ws, wsv, pa, cfg, ref, month, arow, cur_c, path_out)
    col = L(cur_c)
    def num(par):                                               # the cell's figure, else the run's (no stored result)
        v = wsv.cell(arow[par], cur_c).value if par in arow else None
        return v if v is not None else (pa.rows.get(par) or {}).get("cur")
    tree = cfg.get("fashion_tree") or cfg.get("category_tree") or {}
    fc = cfg.get("furniture") or cfg.get("babycare") or {}
    scale = fc.get("items_scale", 1.0)
    rec = [xl.shift_months(month, -i) for i in range(n - 1, -1, -1)]
    yag = [xl.shift_months(m, -12) for m in rec]

    parts: dict[int, tuple[str, float | None]] = {}              # par -> (this month's formula, its figure)
    S = (cfg.get("metrics") or {}).get("shipped_gmv")
    last = lambda par: next((v for m in reversed(ref.months) if m < month
                             and (v := ref.value_by_par(par, m)) is not None), None)
    leaves = [lf for lf in tree.get("leaves", []) if lf.get("items")]
    gexp: dict[int, tuple[str, float]] = {}                      # leaf items par -> (GMV expression, GMV)
    for lf in leaves:
        g = lf.get("gmv")
        if g in arow and num(g) is not None:
            gexp[lf["items"]] = (f"{col}{arow[g]}", num(g))
    gone = [lf for lf in leaves if lf["items"] not in gexp]
    if gone and S in arow and num(S) is not None:
        # No GMV row in this file: roll the share forward from last month (as the masters do), and let the last
        # category take what is left of shipped GMV so the categories still add up to it.
        sc, sv = f"{col}{arow[S]}", num(S)
        for lf in gone[:-1]:
            s_ = last(lf.get("share"))
            if s_ is not None:
                gexp[lf["items"]] = (f"{sc}*{s_:.10g}", sv * s_)
        lf = gone[-1]
        if all(x["items"] in gexp for x in leaves if x is not lf):
            gexp[lf["items"]] = (f"{sc}-" + "-".join(f"({gexp[x['items']][0]})" for x in leaves if x is not lf),
                                 sv - sum(gexp[x["items"]][1] for x in leaves if x is not lf))
    for lf in leaves:
        it, a = lf["items"], lf.get("asp")
        av = num(a)
        ax = f"{col}{arow[a]}" if av is not None and a in arow else None
        if ax is None and (av := last(a)) is not None:                       # ASP rolls forward from last month
            ax = f"{av:.10g}"
        if it in gexp and ax and av:
            gx, gv = gexp[it]
            parts[it] = (f"=({gx})*10^7/({ax})" + (f"/{scale:.0f}" if scale != 1 else ""), gv * 1e7 / av / scale)
    extra: dict[int, float] = {}                                 # the shares / GMV / ASP rows the file did not have
    for lf in leaves:
        g, sh, a = lf.get("gmv"), lf.get("share"), lf.get("asp")
        if lf["items"] in gexp:
            extra[g] = gexp[lf["items"]][1]
            if sh and S in arow and num(S):
                extra[sh] = gexp[lf["items"]][1] / num(S)
        av = num(a) if a in arow else None
        if av is None:
            av = last(a)
        if a and av is not None:
            extra[a] = av
    placed = {t.par_id: None for t, _s in todo}
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
        total, share = t.par_id == fc.get("items_total"), t.unit == "%"
        if t.par_id in extra:
            cur = extra[t.par_id]
        elif total:
            mine = [p for p in parts if p in placed]
            cur = sum(parts[p][1] for p in mine) if mine else None
        elif share:
            cur = None
        else:
            cur = parts.get(t.par_id, (None, None))[1]
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
    tot = fc.get("items_total")
    for t, _sec in todo:                                        # this month's cells become formulas
        cell = ws[f"{col}{arow[t.par_id]}"]
        if t.par_id in parts:
            cell.value = parts[t.par_id][0]
            figures[t.par_id] = parts[t.par_id][1]
        elif t.par_id == tot and tot in arow:
            mine = [f"{col}{arow[p]}" for p in parts]
            if mine:
                cell.value = "=" + "+".join(mine)
        elif t.unit == "%" and tot in arow and figures.get(tot):
            lf = next((x for x in tree.get("leaves", []) if x.get("items_share") == t.par_id), None)
            if lf and lf.get("items") in parts:
                cell.value = f"={col}{arow[lf['items']]}/{col}{arow[tot]}"
                figures[t.par_id] = figures[lf["items"]] / figures[tot]
    if xl.RUN_DATA in wb.sheetnames:                            # so a later edit is measured against these
        rd = wb[xl.RUN_DATA]
        k = rd.max_row + 1
        for t, _s in todo:
            if figures.get(t.par_id) is not None:
                for j, x in enumerate([t.par_id, figures[t.par_id], Provenance.DERIVED.value, "medium",
                                       "Units = GMV x 10^7 / ASP, added to an earlier Analysis file", month], 1):
                    rd.cell(k, j, x)
                k += 1
    curv = {p: num(p) for p in arow}
    curv.update({p: v for p, v in figures.items() if v is not None})
    linked_n = xl._link_formulas(ws, arow, col, curv, cfg, month, ref, keep_formulas=True)
    xl.add_growth_colours(ws, rel_rngs, pp_rngs)
    wb.save(path_out)
    return {"added": len(todo), "linked": linked_n, "path": str(path_out), "player": cfg["display_name"], "month": month}


def fill_history_in_analysis(path_in: str | Path, path_out: str | Path) -> dict[str, Any]:
    """Fill the past-month and year-ago cells an Analysis workbook has left blank.

    Each blank history cell takes the reference's figure for that month, else the one derived from shares
    (category GMV, platform shipped GMV), else, for a category ASP, GMV x 10 / units from the same month.
    Only blank cells are written, so nothing already in the file (your edits included) is changed, and the
    MoM/YoY/average formulas beside them pick the new figures up. The original is never changed."""
    from openpyxl.utils import get_column_letter as L

    pa = read_analysis(path_in)
    month = _month_key(pa.month)
    cfg = load_player_config(pa.player)
    ref = load_reference(resolve_reference(None), cfg)
    wb = openpyxl.load_workbook(path_in)
    ws = wb[pa.analysis_sheet]
    head = [c.value for c in ws[5]]
    label = xl.month_label(month)
    if label not in head:
        raise ValueError(f"cannot find the {label} column in the Analysis sheet")
    n = 6
    cur_c = head.index(label) + 1
    c_rec = cur_c - n + 1
    c_yag = c_rec - n - 1
    if c_yag < 5:
        raise ValueError("this Analysis sheet does not have the standard column layout")
    cols = [(c_yag + j, xl.shift_months(month, -12 - (n - 1 - j))) for j in range(n)] + \
           [(c_rec + j, xl.shift_months(month, -(n - 1 - j))) for j in range(n - 1)]
    dh_all = xl.derived_history(ref, cfg)
    asp_of = {}                                                     # asp par -> (gmv par, units par)
    for lf in (cfg.get("l2_categories") or {}).values():
        g_, a_, u_ = (ref.row_to_par.get(lf.get(k)) for k in ("gmv", "asp", "units"))
        if g_ and a_ and u_:
            asp_of[a_] = (g_, u_)
    filled = rows = 0
    for c in ws["A"][5:]:
        par = c.value
        if not isinstance(par, int):
            continue
        row = ref.par_to_row.get(par)
        dh = dh_all.get(par, {})
        any_ = False
        for col, m in cols:
            cell = ws.cell(c.row, col)
            if cell.value not in (None, ""):
                continue
            v = xl._hist_val(ref, row, dh, m)
            if v is None and par in asp_of:
                g_, u_ = asp_of[par]
                gv = ref.value_by_par(g_, m) if ref.par_to_row.get(g_) else None
                gv = gv if gv is not None else dh_all.get(g_, {}).get(m)
                uv = ref.value_by_par(u_, m) if ref.par_to_row.get(u_) else None
                if gv and uv:
                    v = gv * 10 / uv
            if v is None:
                continue
            cell.value = v
            cell.number_format = ws.cell(c.row, cur_c).number_format
            cell.font = xl.TD
            filled += 1
            any_ = True
        rows += any_
    if not filled:
        raise ValueError("every historical cell that has a figure on record is already filled")
    wb.save(path_out)
    return {"filled": filled, "rows": rows, "path": str(path_out), "player": cfg["display_name"], "month": month}


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
