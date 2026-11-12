"""The monthly Channel Distribution workbook, in the exact layout of the reference sheet.

The reference sheet ("Pepperfry Channel Distribution") holds, per month, Pepperfry's shipped GMV,
the online/offline split and each channel's category mix, followed by five working blocks to the
right (Working, Total, Contribution, Output, Check). Online share x category mix x shipped GMV is
channel GMV by category; summed over both channels and divided by shipped GMV it must equal the
platform's own category shares (Output). The Check block is the gap.

This module takes the sheet itself, keeps every format, width, hidden column and formula, inserts
the new month into the table and into each block, and fills the new column. The category mixes
are solved so that the Check block reconciles to the run's category shares.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.formula.translate import Translator
from openpyxl.utils import column_index_from_string as C_IDX
from openpyxl.utils import get_column_letter as L

from ..domain import month_label
from ..readers.reference import _parse_month_header

SHEET = "Pepperfry Channel Distribution"
HDR_ROW = 4                       # month headers of the main table
SHIPPED, ONLINE, OFFLINE = 5, 6, 7
OFF_ROWS = (13, 14, 15, 16, 17)   # Studio (offline) category mix, in category order
ON_ROWS = (22, 23, 24, 25, 26)    # Online category mix
BALANCE = {16: (13, 14, 15, 17), 26: (22, 23, 24, 25)}   # rows the sheet sets as 1 - the others
BLOCK_LABEL_ROW, BLOCK_HDR_ROWS = 9, (11, 20)
MAX_ROW = 30

_REF = re.compile(r"(\$?)([A-Z]{1,3})(\$?)(\d+)")


# ------------------------------------------------------------------ sheet mechanics

def _shift_formula(f: str, at: int, n: int = 1) -> str:
    """Excel's own rule for inserting a column at `at`: every reference at or right of it moves."""
    def sub(m: re.Match) -> str:
        d1, col, d2, row = m.groups()
        i = C_IDX(col)
        return f"{d1}{L(i + n) if i >= at else col}{d2}{row}"
    parts = re.split(r'("[^"]*")', f)                  # leave string literals alone
    return "".join(p if p.startswith('"') else _REF.sub(sub, p) for p in parts)


def insert_column(ws, at: int) -> None:
    """Insert one empty column at `at`, moving cells, styles, widths and formulas."""
    maxc = ws.max_column
    for r in range(1, ws.max_row + 1):
        for c in range(maxc, at - 1, -1):
            src, dst = ws.cell(r, c), ws.cell(r, c + 1)
            dst.value = src.value
            if src.has_style:
                dst._style = copy.copy(src._style)
            src.value = None
    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.value = _shift_formula(cell.value, at)
    # column widths / hidden flags move with their columns (kept per column in ws._cd)
    cd = ws._cd
    for i in sorted((k for k in cd if k >= at), reverse=True):
        cd[i + 1] = cd[i]
    cd[at] = (cd.get(at - 1, (None, False))[0], False)
    for rng in list(ws.merged_cells.ranges):
        if rng.min_col >= at:
            ws.merged_cells.remove(rng)
            ws.merge_cells(start_row=rng.min_row, start_column=rng.min_col + 1,
                           end_row=rng.max_row, end_column=rng.max_col + 1)


def expand_dims(ws) -> None:
    """Column widths and hidden flags as one entry per column (the file stores them in ranges)."""
    top = ws.max_column + 3
    cd: dict[int, tuple] = {}
    for d in ws.column_dimensions.values():
        for i in range(d.min or 1, min(d.max or d.min or 1, top) + 1):
            cd[i] = (d.width, bool(d.hidden))
    ws._cd = cd


def apply_dims(ws) -> None:
    for k in list(ws.column_dimensions.keys()):
        del ws.column_dimensions[k]
    for i, (w, h) in sorted(ws._cd.items()):
        d = ws.column_dimensions[L(i)]
        d.min = d.max = i
        if w:
            d.width = w
        d.hidden = h


def _copy_column(ws, src: int, dst: int) -> None:
    """Copy a column's styles, and its formulas moved one place, for rows 1..MAX_ROW."""
    for r in range(1, MAX_ROW + 1):
        s, d = ws.cell(r, src), ws.cell(r, dst)
        if s.has_style:
            d._style = copy.copy(s._style)
        v = s.value
        if isinstance(v, str) and v.startswith("="):
            v = Translator(v, origin=s.coordinate).translate_formula(d.coordinate)
        d.value = v


def _blocks(ws) -> list[dict[str, Any]]:
    """The working blocks right of the table: label, first column, last column."""
    out, c, maxc = [], 1, ws.max_column
    labels = {}
    for c in range(1, maxc + 1):
        v = ws.cell(BLOCK_LABEL_ROW, c).value
        if isinstance(v, str) and v.strip() in ("Working", "Total", "Contribution", "Output", "Check"):
            labels[c] = v.strip()
    for first, name in labels.items():
        last = first
        while last + 1 <= maxc and ws.cell(11, last + 1).value is not None:
            last += 1
        out.append({"name": name, "first": first, "last": last})
    return sorted(out, key=lambda b: b["first"])


def _table_months(ws) -> dict[str, int]:
    out = {}
    for c in range(4, ws.max_column + 1):
        v = ws.cell(HDR_ROW, c).value
        if v is None:
            break
        k = _parse_month_header(v)
        if k:
            out[k] = c
    return out


# ---------------------------------------------------------------- reading history

def _num(ws, wsv, r: int, c: int) -> float | None:
    for w in (wsv, ws):
        v = w.cell(r, c).value if w is not None else None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def column_mix(ws, wsv, c: int) -> dict[str, Any]:
    """One month's inputs from the table. A balancing row the sheet leaves as a formula is 1 - the rest."""
    def mix(rows):
        vals = {r: _num(ws, wsv, r, c) for r in rows}
        for r, others in BALANCE.items():
            if r in vals and vals[r] is None and all(vals.get(o) is not None for o in others):
                vals[r] = 1.0 - sum(vals[o] for o in others)
        return [vals[r] for r in rows]
    return {"shipped": _num(ws, wsv, SHIPPED, c), "online": _num(ws, wsv, ONLINE, c),
            "on": mix(ON_ROWS), "off": mix(OFF_ROWS)}


# ------------------------------------------------------------------- the estimate

def solve_mix(shares: list[float], online: float, on0: list[float], off0: list[float]):
    """Channel mixes that reproduce the platform's category shares, as close as possible to last month's.

    Per category i the sheet requires  online x on_i + (1 - online) x off_i = share_i.  Starting
    from last month's mixes, each category's gap is closed by moving both mixes in proportion to
    their own size (a big category moves more in absolute terms, not in relative terms). The
    online mix is then rescaled to total 1 and the studio mix follows from the identity, so both
    total 1 whenever the shares do. Anything the floor at 0.05% prevents shows up in the Check block.
    """
    o = online
    s_tot = sum(shares)
    s = [x / s_tot for x in shares]
    on, off = list(on0), list(off0)
    for _ in range(8):
        d_on, d_off = [], []
        for i in range(len(s)):
            r = s[i] - (o * on[i] + (1 - o) * off[i])
            den = (o * on[i]) ** 2 + ((1 - o) * off[i]) ** 2
            lam = r / den if den > 0 else 0.0
            d_on.append(lam * o * on[i] ** 2)
            d_off.append(lam * (1 - o) * off[i] ** 2)
        on = [max(on[i] + d_on[i], 0.0005) for i in range(len(s))]
        t = sum(on)
        on = [x / t for x in on]
        off = [max((s[i] - o * on[i]) / (1 - o), 0.0005) for i in range(len(s))]
        t = sum(off)
        off = [x / t for x in off]
        gap = max(abs(o * on[i] + (1 - o) * off[i] - s[i]) for i in range(len(s)))
        if gap < 1e-9:
            break
    return on, off, gap


def estimate_inputs(history: list[dict], shipped: float, shares: list[float],
                    override: dict | None = None) -> dict[str, Any]:
    """This month's table inputs from the last months' column mixes and the run's platform figures."""
    override = override or {}
    recent = [h["online"] for h in history[-3:] if h.get("online") is not None]
    online = override.get("online")
    method = "supplied"
    if online is None:
        if not recent:
            raise ValueError("No online share in the channel sheet to carry forward.")
        online = sum(recent) / len(recent)
        method = f"mean of the last {len(recent)} months' online share"
    last = history[-1]
    on0 = override.get("on") or last["on"]
    off0 = override.get("off") or last["off"]
    on, off, gap = solve_mix(shares, online, on0, off0)
    return {"shipped": shipped, "online": online, "online_method": method,
            "on": on, "off": off, "gap": gap, "shares": shares}


# ------------------------------------------------------------------------ writing

def _flatten(wb, wbv, name: str):
    """The sheet alone, its cross-sheet links replaced by their values (they point at tabs not carried)."""
    ws, wsv = wb[name], wbv[name]
    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and cell.value.startswith("=") and "!" in cell.value:
                cell.value = wsv[cell.coordinate].value
    for other in list(wb.sheetnames):
        if other != name:
            del wb[other]
    return ws


def has_channel_sheet(path: str | Path) -> bool:
    try:
        wb = openpyxl.load_workbook(path, read_only=True)
        ok = SHEET in wb.sheetnames
        wb.close()
        return ok
    except Exception:
        return False


def last_month(path: str | Path) -> str | None:
    wb = openpyxl.load_workbook(path, data_only=True)
    try:
        if SHEET not in wb.sheetnames:
            return None
        ms = _table_months(wb[SHEET])
        return max(ms) if ms else None
    finally:
        wb.close()


def extract_channel_sheet(master: str | Path, dst: str | Path) -> Path:
    """Keep only the channel sheet of the master workbook, formats and formulas intact."""
    wb, wbv = openpyxl.load_workbook(master), openpyxl.load_workbook(master, data_only=True)
    _flatten(wb, wbv, SHEET)
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    wb.save(dst)
    return Path(dst)


def build_channel_workbook(src: str | Path, month: str, shipped: float, shares: list[float],
                           out_path: str | Path, override: dict | None = None) -> dict[str, Any]:
    """Write the month's Channel Distribution workbook. Returns what was estimated, for the run record."""
    wb, wbv = openpyxl.load_workbook(src), openpyxl.load_workbook(src, data_only=True)
    ws = _flatten(wb, wbv, SHEET)
    wsv = wbv[SHEET] if SHEET in wbv.sheetnames else None

    expand_dims(ws)
    months = _table_months(ws)
    ordered = sorted(months)
    if not ordered:
        raise ValueError("The channel sheet has no month headers on row 4.")
    replacing = month in months
    if replacing and month != ordered[-1]:
        raise ValueError(f"{month_label(month)} is already in the channel sheet and is not its last "
                         f"month ({month_label(ordered[-1])}); rebuild from an earlier copy.")
    if not replacing and _gap(ordered[-1], month) != 1:
        raise ValueError(f"The channel sheet ends at {month_label(ordered[-1])}; "
                         f"{month_label(month)} can only follow the month right after it.")

    prior = [m for m in ordered if m < month]
    history = [column_mix(ws, wsv, months[m]) for m in prior]
    history = [h for h in history if h["online"] is not None and None not in h["on"] + h["off"]]
    est = estimate_inputs(history, shipped, shares, override)

    label = month_label(month)
    if replacing:
        col = months[month]
    else:
        last_col = months[ordered[-1]]
        col = last_col + 1
        insert_column(ws, col)
        _copy_column(ws, last_col, col)
        done = set()
        while True:
            todo = [b for b in _blocks(ws) if b["name"] not in done]
            if not todo:
                break
            b = todo[0]
            done.add(b["name"])
            insert_column(ws, b["last"] + 1)
            _copy_column(ws, b["last"], b["last"] + 1)
            for r in (11, 20):
                ws.cell(r, b["last"] + 1).value = label
            ws.cell(12, b["last"] + 1).value = ws.cell(12, b["first"]).value
    ws.cell(HDR_ROW, col).value = label
    ws.cell(12, col).value = label
    ws.cell(21, col).value = label

    ws.cell(SHIPPED, col).value = round(est["shipped"], 6)
    ws.cell(ONLINE, col).value = round(est["online"], 4)
    ws.cell(OFFLINE, col).value = f"=1-{L(col)}{ONLINE}"
    for rows, mix in ((OFF_ROWS, est["off"]), (ON_ROWS, est["on"])):
        for r, v in zip(rows, mix):
            others = BALANCE.get(r)
            ws.cell(r, col).value = (f"=1-SUM({L(col)}{others[0]}:{L(col)}{others[2]},{L(col)}{others[3]})"
                                     if r == 16 else
                                     f"=1-SUM({L(col)}{others[0]}:{L(col)}{others[3]})" if r == 26
                                     else round(v, 4))

    # the Output block holds the run's platform category shares for the month
    for b in _blocks(ws):
        if b["name"] != "Output":
            continue
        oc = b["last"] if not replacing else _block_col(ws, b, label)
        for r, v in zip(OFF_ROWS, shares):
            ws.cell(r, oc).value = round(v, 6)
        ws.cell(12, oc).value = "Output"

    apply_dims(ws)
    wb.calculation.fullCalcOnLoad = True
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)

    # what the Check block will show: the gap between the channel mixes and the platform shares
    implied = [est["online"] * est["on"][i] + (1 - est["online"]) * est["off"][i] for i in range(5)]
    est["implied"] = implied
    est["check"] = [shares[i] / sum(shares) - implied[i] for i in range(5)]
    est["month"], est["replaced"] = month, replacing
    return est


def _block_col(ws, block: dict, label: str) -> int:
    for c in range(block["first"], block["last"] + 1):
        if ws.cell(11, c).value == label:
            return c
    return block["last"]


def _gap(a: str, b: str) -> int:
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])


# ------------------------------------------------------------- the monthly run

BUILTIN = Path(__file__).resolve().parent.parent.parent / "reference" / "Pepperfry Channel Distribution.xlsx"


def saved_path(data_root: str | Path, key: str) -> Path:
    return Path(data_root) / "channel" / f"{key}.xlsx"


def choose_source(month: str, reference_path: str | None, data_root: str | Path, key: str):
    """The sheet to continue from: whichever carries the month before this one (or this one, to redo it).

    Preference on a tie: the uploaded master, then the copy saved by the last run, then the built-in.
    """
    from ..domain import shift_months
    prior = shift_months(month, -1)
    cands = []
    if reference_path and has_channel_sheet(reference_path):
        cands.append((reference_path, 3, "the uploaded workbook"))
    sp = saved_path(data_root, key)
    if sp.exists():
        cands.append((str(sp), 2, "the copy saved by the last run"))
    if BUILTIN.exists():
        cands.append((str(BUILTIN), 1, "the built-in channel sheet"))
    ok = []
    for path, pri, label in cands:
        last = last_month(path)
        if last in (prior, month):
            ok.append((last, pri, path, label))
    if not ok:
        have = ", ".join(f"{label} ends {month_label(last_month(p))}" for p, _, label in cands
                         if last_month(p)) or "no channel sheet found"
        raise ValueError(f"No channel sheet ends at {month_label(prior)} ({have}).")
    ok.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return ok[0][2], ok[0][3]


def run_channel(cfg: dict, est, ref, month: str, out_dir: Path, base: str,
                reference_path: str | None, data_root: str | Path) -> tuple[Path | None, dict]:
    """Build this run's Channel Distribution workbook. (None, {}) for a player that has no channel sheet."""
    cc = cfg.get("channel_distribution")
    if not cc:
        return None, {}
    shipped = est.value_for_par(ref, cc.get("shipped_par", 243))
    share_vals = [est.value_for_par(ref, p) for p in cc["share_pars"]]
    if shipped is None or shipped.value is None or any(v is None or v.value is None for v in share_vals):
        raise ValueError("Shipped GMV or a category share is missing from this run, so the channel "
                         "split cannot be reconciled to it.")
    src, label = choose_source(month, reference_path, data_root, cfg["player"])
    out = Path(out_dir) / f"{base} - Channel Distribution.xlsx"
    info = build_channel_workbook(src, month, shipped.value, [v.value for v in share_vals], out,
                                  cc.get("override"))
    info["source"] = label
    return out, info
