"""One workbook that sets every player's month side by side, for comparing players.

Sheets: Summary (headline figures per player), Values (every metric, players across), MoM and
YoY (growth for every player), and Sources (how each figure was set). Values are coloured low
to high along each row, growth red to green, so the outliers show.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ..domain import month_label, shift_months
from .workbook import (BOX, FILL_BAND, FILL_HEAD, GROUP, H1, H2, TD, TD_B, TD_MUTED, TH, DOT,
                       RYG_GREEN, RYG_RED, RYG_YELLOW, add_growth_colours, ordered_template)


def _first_cols(ws, row: int, labels: list[str]) -> None:
    for i, h in enumerate(labels, 1):
        c = ws.cell(row, i, h)
        c.font, c.fill = TH, FILL_HEAD
        c.alignment = Alignment(horizontal="center" if i > 3 else "left", vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 30


def write_collated_workbook(path: str | Path, results: list, month: str) -> Path:
    """`results` are finished RunResults (one per player) for the same month."""
    results = [r for r in results if r is not None and r.estimate is not None]
    label = month_label(month)
    prior, yago = shift_months(month, -1), shift_months(month, -12)
    wb = Workbook()
    names = [r.config["display_name"] if r.config else r.player for r in results]

    # rows: the output sheet's order, from the first player; keep rows any player has a figure for
    base = results[0]
    order = [t for t, sec in ordered_template(base.reference, base.estimate,
                                              base.config) if sec != "Not available this month"]
    seen = {t.par_id for t in order}
    for r in results[1:]:
        for t, sec in ordered_template(r.reference, r.estimate, r.config):
            if t.par_id not in seen and sec != "Not available this month":
                order.append(t)
                seen.add(t.par_id)

    def val(r, par):
        v = r.estimate.value_for_par(r.reference, par)
        return v if v is not None and not v.is_null and v.value is not None else None

    def hist(r, par, m):
        row = r.reference.par_to_row.get(par)
        return r.reference.value(row, m) if row else None

    rows = [t for t in order if any(val(r, t.par_id) is not None for r in results)]

    # ---- Summary -----------------------------------------------------------
    ws = wb.active
    ws.title = "Summary"
    ws.sheet_view.showGridLines = False
    ws.cell(1, 1, f"All players, {label}").font = H1
    ws.cell(2, 1, "Gross GMV, shipped GMV, AOV and ASP for each player, with growth on the stored "
                  "history. See Values, MoM and YoY for every metric.").font = H2
    head = ["Player", "Gross GMV (INR Cr)", "MoM", "YoY", "Shipped GMV (INR Cr)", "AOV (INR)",
            "ASP (INR)", "Built from", "Confidence", "Errors", "Warnings"]
    _first_cols(ws, 4, head)
    for i, w in enumerate([22, 16, 9, 9, 18, 12, 12, 22, 12, 8, 9], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    rr = 5
    for name, r in zip(names, results):
        g = val(r, 241)
        gp, gy = hist(r, 241, prior), hist(r, 241, yago)
        files = [k for k in ("mis", "transcript") if (r.mis if k == "mis" else r.transcript)]
        row = [name, g.value if g else None,
               (g.value / gp - 1) if g and gp else None, (g.value / gy - 1) if g and gy else None,
               val(r, 243).value if val(r, 243) else None, val(r, 240).value if val(r, 240) else None,
               val(r, 468).value if val(r, 468) else None,
               " + ".join({"mis": "MIS", "transcript": "call"}[k] for k in files) or "history only",
               r.weights.confidence.value if r.weights else "", len(r.errors), len(r.warnings)]
        for i, v in enumerate(row, 1):
            c = ws.cell(rr, i, v)
            c.font, c.border = (TD_B if i == 1 else TD), BOX
            if i in (2, 5):
                c.number_format = "#,##0.0"
            elif i in (3, 4):
                c.number_format = "+0.0%;-0.0%"
            elif i in (6, 7):
                c.number_format = "#,##0"
            if i > 1:
                c.alignment = Alignment(horizontal="right" if i < 8 else "center")
            if rr % 2:
                c.fill = FILL_BAND
        rr += 1
    ws.freeze_panes = "B5"
    add_growth_colours(ws, [f"C5:D{rr - 1}"], [], rel_span=0.30)

    # ---- Values / MoM / YoY / Sources -----------------------------------------
    sheets = {k: wb.create_sheet(k) for k in ("Values", "MoM", "YoY", "Sources")}
    notes = {"Values": f"Each player's figure for {label}. Shading runs low to high along each row.",
             "MoM": f"{label} against {month_label(prior)}, from each player's stored history. "
                    "Percentage metrics: change in points.",
             "YoY": f"{label} against {month_label(yago)}. Percentage metrics: change in points.",
             "Sources": "How each figure was set: reported, call, derived, estimated, calibrated or assumed."}
    for k, s in sheets.items():
        s.sheet_view.showGridLines = False
        s.cell(1, 1, f"{k}, {label}").font = H1
        s.cell(2, 1, notes[k]).font = H2
        _first_cols(s, 4, ["par_id", "Parameter", "Unit"] + names)
        s.column_dimensions["A"].width, s.column_dimensions["B"].width, s.column_dimensions["C"].width = 8, 46, 9
        for j in range(len(names)):
            s.column_dimensions[get_column_letter(4 + j)].width = 14
        s.freeze_panes = "D5"

    rel: dict[str, list[str]] = {"MoM": [], "YoY": []}
    pp: dict[str, list[str]] = {"MoM": [], "YoY": []}
    line = 5
    last_q = None
    for t in rows:
        pct = t.unit == "%"
        for k, s in sheets.items():
            for i, v in enumerate([t.par_id, t.parameter_name.strip(), t.unit or ""], 1):
                c = s.cell(line, i, v)
                c.font, c.border = (TD_MUTED if i == 1 else TD), BOX
                if line % 2:
                    c.fill = FILL_BAND
        for j, r in enumerate(results):
            col = 4 + j
            v = val(r, t.par_id)
            cur = v.value if v else None
            c = sheets["Values"].cell(line, col, cur)
            c.number_format = "0.00%" if pct else ("#,##0.00" if (t.unit or "") in ("INR Cr", "Mn", "#", "") else "#,##0")
            c.font, c.border = TD, BOX
            for k, m in (("MoM", prior), ("YoY", yago)):
                h = hist(r, t.par_id, m)
                if cur is None or h is None or (not pct and not h):
                    g = None
                else:
                    g = (cur - h) * 100 if pct else cur / h - 1
                c = sheets[k].cell(line, col, g)
                c.number_format = '0.0" pp"' if pct else "0.0%"
                c.font, c.border = TD, BOX
            if v:
                kind, color = DOT[v.provenance]
                c = sheets["Sources"].cell(line, col, kind)
                c.font, c.border = Font(name="Calibri", size=10, color=color), BOX
                c.alignment = Alignment(horizontal="center")
        rng = f"{get_column_letter(4)}{line}:{get_column_letter(3 + len(results))}{line}"
        (pp if pct else rel)["MoM"].append(rng)
        (pp if pct else rel)["YoY"].append(rng)
        sheets["Values"].conditional_formatting.add(rng, ColorScaleRule(
            start_type="min", start_color=RYG_RED, mid_type="percentile", mid_value=50,
            mid_color=RYG_YELLOW, end_type="max", end_color=RYG_GREEN))
        line += 1
    for k in ("MoM", "YoY"):
        add_growth_colours(sheets[k], rel[k], pp[k])

    wb.save(path)
    return Path(path)
