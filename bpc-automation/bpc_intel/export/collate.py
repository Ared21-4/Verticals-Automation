"""Collate several players' Analysis workbooks into one workbook with a tab per player.

Reads the files the system itself writes (`... - Analysis.xlsx`), so it works on any set of
them, from any month. The result has a Summary, three side-by-side comparison tabs
(values, MoM, YoY for the latest month in each file) and, for each player, that player's
Analysis tab and its full History tab, copied as they are.
"""

from __future__ import annotations

import re
from copy import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl import Workbook
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter

from .workbook import (BOX, FILL_BAND, FILL_HEAD, H1, H2, TD, TD_B, TD_MUTED, TH, add_growth_colours, RYG_GREEN, RYG_RED, RYG_YELLOW)


@dataclass
class PlayerAnalysis:
    path: str
    player: str
    month: str                                   # e.g. "Sep'26", as the file labels it
    rows: dict[int, dict[str, Any]] = field(default_factory=dict)    # par_id -> label, unit, cur, prior, yago
    has_history: bool = False
    history_sheet: str | None = None      # the name this file gives its History sheet
    analysis_sheet: str = "Analysis"
    keyed: bool = False         # the file already opens with a Key metrics block
    uncached: int = 0           # formula cells that carry no stored result (file saved by a library, not Excel)


_OTHER_SHEETS = ("mom", "yoy", "vs ", "quarterly", "half", "deviation", "run data", "summary", "compare")


def _find_sheet(wb, word: str, header_row: int, min_months: int) -> str | None:
    """The sheet that plays the part of `word` ("Analysis" or "History"), even if it was renamed.

    By exact name, then ignoring case and spaces, then a name that contains the word ("Mamaearth
    history", "History (2)"), and last by what the sheet holds: a par_id header row with month columns.
    """
    from ..readers.reference import _parse_month_header
    names = wb.sheetnames
    plain = [n for n in names if not any(o in n.lower() for o in _OTHER_SHEETS)]
    if word in names:
        return word
    for n in plain:
        if n.strip().lower() == word.lower():
            return n
    for n in plain:
        if word.lower() in n.lower():
            return n
    for n in plain:
        ws = wb[n]
        r = _header_row(ws)
        head = [c.value for c in ws[r]] if r else []
        if head and sum(1 for v in head[4:] if _parse_month_header(v)) >= min_months:
            return n
    return None


def _header_row(ws, upto: int = 8) -> int | None:
    """The row that heads the sheet's columns: "par_id" in column A."""
    for r in range(1, min(ws.max_row, upto) + 1):
        if str(ws.cell(r, 1).value or "").strip().lower() == "par_id":
            return r
    return None


def read_analysis(path: str | Path) -> PlayerAnalysis:
    """Read one Analysis workbook. Raises ValueError if it is not one."""
    wb = openpyxl.load_workbook(path, data_only=False)
    a_name = _find_sheet(wb, "Analysis", 5, 3)
    if a_name is None:
        raise ValueError("no 'Analysis' sheet (the file has: " + ", ".join(wb.sheetnames[:8]) + ")")
    ws = wb[a_name]
    # A cell the analyst edited may hold a formula (=K10*1.05). Its figure is the result Excel stored
    # beside it, so read the workbook a second time for values.
    ws_v = openpyxl.load_workbook(path, data_only=True)[a_name]
    # A file the portal has just written holds formulas with no stored result until Excel opens it;
    # the "Run data" sheet keeps the figures the run produced, which they equal until edited.
    from . import workbook as _wbk
    run_orig: dict[int, Any] = {}
    if getattr(_wbk, "RUN_DATA", "Run data") in wb.sheetnames:
        run_orig = {r_[0]: r_[1] for r_ in wb[getattr(_wbk, "RUN_DATA", "Run data")].iter_rows(
            min_row=2, values_only=True) if isinstance(r_[0], int)}
    title = str(ws.cell(1, 1).value or "")
    m = re.match(r"\s*(.+?):\s*(.+?)\s+against\b", title)
    if not m:
        raise ValueError("the first cell is not an analysis title")
    player, month = m.group(1).strip(), m.group(2).strip()

    head = [c.value for c in ws[5]]
    blocks, cur_block = [], []
    for i, h in enumerate(head[4:], start=5):
        if h in (None, ""):
            if cur_block:
                blocks.append(cur_block)
                cur_block = []
        else:
            cur_block.append(i)
    if cur_block:
        blocks.append(cur_block)
    if len(blocks) < 2 or len(blocks[0]) < 2 or len(blocks[1]) < 2:
        raise ValueError("the month columns were not found")
    yag, rec = blocks[0], blocks[1]
    c_cur, c_prior, c_yag = rec[-1], rec[-2], yag[-1]

    h_name = _find_sheet(wb, "History", 4, 6)
    pa = PlayerAnalysis(str(path), player, month, has_history=h_name is not None,
                        history_sheet=h_name, analysis_sheet=a_name)
    group = ""
    for row in ws.iter_rows(min_row=6):
        par = row[0].value
        if not isinstance(par, int):
            # a heading row: a plain one names the question the rows below answer, a shaded one is a section
            if row[2].value and row[0].fill.fill_type != "solid":
                group = str(row[2].value).strip()
            elif str(row[2].value or "").strip() == "Key metrics":
                pa.keyed = True
            continue

        def num(c, _row=row, _par=par):
            v = _row[c - 1].value
            if isinstance(v, str) and v.startswith("="):
                v = ws_v.cell(_row[0].row, c).value
                if v is None and c == c_cur and run_orig.get(_par) is not None:
                    v = run_orig[_par]
                elif v is None:
                    pa.uncached += 1
            return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        pa.rows[par] = {"label": row[2].value, "group": group, "unit": row[3].value or "",
                        "cur_formula": isinstance(row[c_cur - 1].value, str) and row[c_cur - 1].value.startswith("="),
                        "cur": num(c_cur), "prior": num(c_prior), "yago": num(c_yag)}
    return pa


def _copy_sheet(src, dst, history: str | None = None) -> None:
    for row in src.iter_rows():
        for c in row:
            val = c.value
            if history and isinstance(val, str) and val.startswith("=") and "History!" in val:
                val = val.replace("History!", f"'{history}'!")
            n = dst.cell(c.row, c.column, val)
            if c.has_style:
                n.font, n.fill, n.border = copy(c.font), copy(c.fill), copy(c.border)
                n.alignment, n.number_format = copy(c.alignment), c.number_format
    for k, d in src.column_dimensions.items():
        dst.column_dimensions[k].width = d.width
    for k, d in src.row_dimensions.items():
        if d.height:
            dst.row_dimensions[k].height = d.height
    for rng in src.merged_cells.ranges:
        dst.merge_cells(str(rng))
    dst.freeze_panes = src.freeze_panes
    dst.sheet_view.showGridLines = False
    for cf in src.conditional_formatting:
        for rule in cf.rules:
            dst.conditional_formatting.add(str(cf.sqref), rule)


# The headline pars every analysis opens with. A file written before the analysis sheets put key
# metrics first still gets them on top here.
KEY_PARS = (241, 243, 247, 240, 468, 242, 244, 7301, 245, 246, 238)


def _key_first(pars: list[int]) -> list[int]:
    head = [p for p in KEY_PARS if p in pars]
    return head + [p for p in pars if p not in head]


def _player_sheet(ws, src, pa: "PlayerAnalysis") -> None:
    """One sheet for one player: every stored month, then MoM and YoY across the same months.

    Built from the file's History sheet (the figures) with growth written as formulas, so a figure
    changed here updates its MoM and YoY. Percentage metrics show growth in points.
    """
    from ..domain import shift_months
    from ..readers.reference import _parse_month_header
    from .workbook import FILL_GROUP, GROUP

    h = src[pa.history_sheet]
    L = get_column_letter
    hr = _header_row(h) or 4                                   # the History sheet's header row
    mcols = [(i + 1, _parse_month_header(v)) for i, v in enumerate([c.value for c in h[hr]])
             if i >= 4 and _parse_month_header(v)]
    if not mcols:
        raise ValueError("History has no month columns")
    n = len(mcols)
    c_val, c_mom = 5, 5 + n + 1
    c_yoy = c_mom + n + 1
    pos = {key: c_val + j for j, (_, key) in enumerate(mcols)}
    width = c_yoy + n - 1

    ws.sheet_view.showGridLines = False
    ws.cell(1, 1, f"{pa.player}: {h.cell(hr, mcols[0][0]).value} to {h.cell(hr, mcols[-1][0]).value}, "
                  "with MoM and YoY").font = H1
    ws.cell(2, 1, "Key metrics first. Growth is a formula on the figures: change a figure and it updates. "
                  "Percentage metrics show growth in percentage points (pp).").font = H2
    for c0, label in ((c_val, "Values"), (c_mom, "MoM growth"), (c_yoy, "YoY growth")):
        cell = ws.cell(4, c0, label)
        cell.font, cell.fill = GROUP, FILL_GROUP
        cell.alignment = Alignment(horizontal="center")
        ws.merge_cells(start_row=4, start_column=c0, end_row=4, end_column=c0 + n - 1)
    heads = ["par_id", "S.No", "Parameter", "Units"]
    month_labels = [str(h.cell(hr, sc).value) for sc, _ in mcols]
    for i, lab in enumerate(heads + month_labels + [""] + month_labels + [""] + month_labels, 1):
        c = ws.cell(5, i, lab if lab else None)
        if lab:
            c.font, c.fill = TH, FILL_HEAD
            c.alignment = Alignment(horizontal="center" if i > 4 else "left")
    for i, w in {1: 8, 2: 6, 3: 56, 4: 9}.items():
        ws.column_dimensions[L(i)].width = w
    for i in range(5, width + 1):
        ws.column_dimensions[L(i)].width = 2 if i in (c_mom - 1, c_yoy - 1) else 10
    ws.freeze_panes = "E6"

    rel: list[str] = []
    pp: list[str] = []
    src_rows = list(range(hr + 1, h.max_row + 1))
    first_banner = next((str(h.cell(x, 3).value or "").strip() for x in src_rows
                         if h.cell(x, 3).value and not isinstance(h.cell(x, 1).value, int)), "")
    if first_banner != "Key metrics":            # an older file: lift the headline rows to the top
        by_par = {h.cell(x, 1).value: x for x in src_rows if isinstance(h.cell(x, 1).value, int)}
        top = [by_par[p] for p in KEY_PARS if p in by_par]
        src_rows = [0] + top + [x for x in src_rows if x not in top]          # 0 = a "Key metrics" banner
    for k, r_src in enumerate(src_rows):
        r = 6 + k
        if r_src == 0:
            for col in range(1, width + 1):
                ws.cell(r, col).fill = FILL_GROUP
            ws.cell(r, 3, "Key metrics").font = GROUP
            continue
        par, unit = h.cell(r_src, 1).value, h.cell(r_src, 4).value
        for col in range(1, 5):
            s = h.cell(r_src, col)
            d = ws.cell(r, col, s.value)
            if s.has_style:
                d.font, d.fill, d.border = copy(s.font), copy(s.fill), copy(s.border)
                d.alignment, d.number_format = copy(s.alignment), s.number_format
        if not isinstance(par, int):                           # a section banner
            for col in range(1, width + 1):
                ws.cell(r, col).fill = FILL_GROUP
            continue
        pct = unit == "%"
        gfmt = '0.0" pp"' if pct else "0.0%"
        for j, (sc, key) in enumerate(mcols):
            s = h.cell(r_src, sc)
            d = ws.cell(r, c_val + j, s.value)
            d.font, d.number_format = copy(s.font), s.number_format
            cur = f"{L(c_val + j)}{r}"
            for base_col, back in ((c_mom, -1), (c_yoy, -12)):
                pk = shift_months(key, back)
                if pk not in pos:
                    continue
                prev = f"{L(pos[pk])}{r}"
                f_ = (f'=IF(AND(ISNUMBER({cur}),ISNUMBER({prev})),({cur}-{prev})*100,"")' if pct else
                      f'=IF(AND(ISNUMBER({cur}),ISNUMBER({prev}),{prev}<>0),{cur}/{prev}-1,"")')
                g = ws.cell(r, base_col + j, f_)
                g.number_format, g.font = gfmt, TD
        for col in range(1, width + 1):
            if col not in (c_mom - 1, c_yoy - 1):
                ws.cell(r, col).border = BOX
        (pp if pct else rel).extend([f"{L(c_mom)}{r}:{L(c_mom + n - 1)}{r}",
                                     f"{L(c_yoy)}{r}:{L(c_yoy + n - 1)}{r}"])
    add_growth_colours(ws, rel, pp)


def _tab(name: str, taken: set[str]) -> str:
    base = re.sub(r"[\[\]:*?/\\]", " ", name)[:31].strip() or "Player"
    out, n = base, 2
    while out in taken:
        out = f"{base[:28]} {n}"
        n += 1
    taken.add(out)
    return out


def collate_analysis_files(paths: list[str | Path], out_path: str | Path) -> tuple[Path, list[str]]:
    """Build the collated workbook. Returns (path, problems): files that could not be used."""
    problems: list[str] = []
    players: list[PlayerAnalysis] = []
    seen: dict[tuple[str, str], str] = {}
    for p in paths:
        try:
            pa = read_analysis(p)
        except Exception as e:
            problems.append(f"{Path(p).name}: not used ({e}).")
            continue
        key = (pa.player.lower(), pa.month)
        if key in seen:
            problems.append(f"{Path(p).name}: {pa.player} {pa.month} appears twice; using the later file.")
            players = [x for x in players if (x.player.lower(), x.month) != key]
        if pa.uncached:
            problems.append(f"{Path(p).name}: {pa.uncached} cell(s) hold a formula with no stored result, so "
                            "they are blank here. Open the file in Excel and save it, then upload it again.")
        seen[key] = str(p)
        players.append(pa)
    if not players:
        raise ValueError("None of the files could be read as an Analysis workbook.")
    months = {pa.month for pa in players}
    if len(months) > 1:
        problems.append("The files are for different months (" + ", ".join(sorted(months)) +
                        "); the comparison tabs set each player's latest month side by side.")

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.sheet_view.showGridLines = False
    ws.cell(1, 1, "All players: analysis").font = H1
    ws.cell(2, 1, "One tab per player follows the comparison tabs. Growth on the comparison tabs "
                  "is each player's latest month against the month before and the same month last year.").font = H2
    for i, (h, w) in enumerate([("Player", 24), ("Month", 12), ("Metrics", 10), ("Tabs", 40), ("File", 60)], 1):
        c = ws.cell(4, i, h)
        c.font, c.fill = TH, FILL_HEAD
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    taken = {"Summary", "Compare values", "Compare MoM", "Compare YoY"}
    tabs: dict[str, list[str]] = {}
    for pa in players:
        tabs[pa.path] = [_tab(pa.player, taken)]               # one sheet per player
    for r, pa in enumerate(players, start=5):
        for i, v in enumerate([pa.player, pa.month, len(pa.rows), ", ".join(tabs[pa.path]),
                               Path(pa.path).name], 1):
            c = ws.cell(r, i, v)
            c.font, c.border = (TD_B if i == 1 else TD), BOX
            if r % 2:
                c.fill = FILL_BAND
    r = 5 + len(players) + 1
    for pr in problems:
        ws.cell(r, 1, pr).font = TD_MUTED
        r += 1

    # ---- comparison tabs ----------------------------------------------------
    order: list[int] = []
    for pa in players:
        mine = list(pa.rows) if pa.keyed else _key_first(list(pa.rows))    # an older file: key metrics up
        order += [p for p in mine if p not in order]
    spec = {"Compare values": "The latest month's figure for each player. Shading runs low to high along a row.",
            "Compare MoM": "Latest month against the month before. Percentage metrics: change in points.",
            "Compare YoY": "Latest month against the same month a year earlier. Percentage metrics: change in points."}
    rel_r: list[str] = []
    pp_r: list[str] = []
    sheets = {}
    for k, note in spec.items():
        s = wb.create_sheet(k)
        sheets[k] = s
        s.sheet_view.showGridLines = False
        s.cell(1, 1, k).font = H1
        s.cell(2, 1, note).font = H2
        for i, h in enumerate(["par_id", "Parameter", "Unit"] + [f"{pa.player} ({pa.month})" for pa in players], 1):
            c = s.cell(4, i, h)
            c.font, c.fill = TH, FILL_HEAD
            c.alignment = Alignment(horizontal="center" if i > 3 else "left", wrap_text=True)
        s.column_dimensions["A"].width, s.column_dimensions["B"].width, s.column_dimensions["C"].width = 8, 56, 9
        for j in range(len(players)):
            s.column_dimensions[get_column_letter(4 + j)].width = 16
        s.freeze_panes = "D5"
    line = 5
    for par in order:
        meta = next(pa.rows[par] for pa in players if par in pa.rows)
        pct = meta["unit"] == "%"
        if not any(pa.rows.get(par, {}).get("cur") is not None for pa in players):
            continue
        for k, s in sheets.items():
            label = str(meta["label"] or "")
            grp = meta.get("group") or ""
            if grp and grp.lower() not in label.lower():
                label = f"{label} | {grp}"                    # e.g. "Grooming(%) | Category - Shipped % GMV"
            for i, v in enumerate([par, label, meta["unit"]], 1):
                c = s.cell(line, i, v)
                c.font, c.border = (TD_MUTED if i == 1 else TD), BOX
        for j, pa in enumerate(players):
            d = pa.rows.get(par)
            cur = d["cur"] if d else None
            vals = {"Compare values": cur}
            for k, base in (("Compare MoM", d["prior"] if d else None), ("Compare YoY", d["yago"] if d else None)):
                vals[k] = None if cur is None or base is None or (not pct and not base) else \
                    ((cur - base) * 100 if pct else cur / base - 1)
            for k, s in sheets.items():
                c = s.cell(line, 4 + j, vals[k])
                c.border = BOX
                c.font = TD
                c.number_format = ("0.00%" if pct else "#,##0.00") if k == "Compare values" else \
                    ('0.0" pp"' if pct else "0.0%")
        rng = f"D{line}:{get_column_letter(3 + len(players))}{line}"
        (pp_r if pct else rel_r).append(rng)
        sheets["Compare values"].conditional_formatting.add(rng, ColorScaleRule(
            start_type="min", start_color=RYG_RED, mid_type="percentile", mid_value=50,
            mid_color=RYG_YELLOW, end_type="max", end_color=RYG_GREEN))
        line += 1
    for k in ("Compare MoM", "Compare YoY"):
        add_growth_colours(sheets[k], rel_r, pp_r)

    # ---- one tab per player --------------------------------------------------
    for pa in players:
        src = openpyxl.load_workbook(pa.path)
        name = tabs[pa.path][0]
        if pa.has_history:
            _player_sheet(wb.create_sheet(name), src, pa)
        else:                                                    # an older file with no History sheet
            _copy_sheet(src[pa.analysis_sheet], wb.create_sheet(name))
            problems.append(f"{Path(pa.path).name}: no History sheet found (its sheets are "
                            f"{', '.join(src.sheetnames[:8])}), so its Analysis tab is copied as it is.")

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return Path(out_path), problems
