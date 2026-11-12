"""Fill a blank webform (the portal's CSV layout) from a month's Output workbook.

The webform is keyed by par_id. Each row takes the Output's value for that par_id, written the way the
filled webforms already are (ten decimals, ``0E-10`` for zero). A par_id the Output has no figure for is
written as ``null``. Percentages are already in percent points in the Output, as in the webform.
"""

from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "config" / "webform_template.csv"

import openpyxl

TEN = Decimal("1E-10")


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v).strip())
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def output_values(output_path) -> dict[int, Decimal]:
    """par_id -> value from the Output workbook's upload sheet (first figure wins when a par repeats)."""
    wb = openpyxl.load_workbook(output_path, data_only=True, read_only=True)
    ws = wb["BPC-WF"] if "BPC-WF" in wb.sheetnames else wb[wb.sheetnames[0]]
    out: dict[int, Decimal] = {}
    for r in ws.iter_rows(values_only=True):
        if len(r) < 5 or r[2] is None:
            continue
        try:
            par = int(r[2])
        except (TypeError, ValueError):
            continue
        d = _num(r[4])
        if d is not None and par not in out:
            out[par] = d
    wb.close()
    return out


def _read_template(path) -> list[list[str]]:
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        ws = openpyxl.load_workbook(p, data_only=True).worksheets[0]
        return [["" if c is None else str(c) for c in r[:5]] for r in ws.iter_rows(values_only=True)]
    with open(p, newline="", encoding="utf-8-sig") as f:
        return [r for r in csv.reader(f)]


def output_label(output_path) -> tuple[str, str]:
    """(player, month label) from the Output's title cell, e.g. "nykaa — Oct'26" -> ("nykaa", "Oct'26")."""
    wb = openpyxl.load_workbook(output_path, read_only=True)
    ws = wb["BPC-WF"] if "BPC-WF" in wb.sheetnames else wb[wb.sheetnames[0]]
    head = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    wb.close()
    month = str(head[4] or "").strip() if len(head) > 4 else ""
    contents = openpyxl.load_workbook(output_path, read_only=True)
    title = str(next(contents["Contents"].iter_rows(min_row=1, max_row=1, values_only=True))[0] or "") if "Contents" in contents.sheetnames else ""
    contents.close()
    return title.split(" — ")[0].strip(), month


def fill_webform(output_path, webform_path=None, out_path=None) -> dict:
    """Write the filled webform CSV to ``out_path``; return how many rows were filled or left null."""
    vals = output_values(output_path)
    rows = _read_template(webform_path or TEMPLATE)
    if not rows or [h.strip() for h in rows[0][:4]] != ["parent_question", "parameter_name", "par_id", "unit"]:
        raise ValueError("The webform must have the columns parent_question, parameter_name, par_id, unit, Month.")
    filled = nulls = 0
    out = [rows[0][:5]]
    for r in rows[1:]:
        r = (r + [""] * 5)[:5]
        try:
            d = vals.get(int(r[2]))
        except ValueError:
            d = None
        if d is None:
            r[4] = "null"
            nulls += 1
        else:
            r[4] = str(d.quantize(TEN))
            filled += 1
        out.append(r)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f, lineterminator="\r\n").writerows(out)
    return {"rows": len(out) - 1, "filled": filled, "null": nulls, "path": str(out_path)}
