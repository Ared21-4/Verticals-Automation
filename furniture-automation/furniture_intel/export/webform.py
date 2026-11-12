"""Fill a player's webform (the portal's CSV layout) from a month's Output workbook.

The webform is keyed by par_id and is the same for every Fashion player: its blank layout is saved in
``config/webforms/webform.csv``. Each row takes the Output's value for its par_id, written as the filled webforms are (ten decimals,
``0E-10`` for zero); a par_id the Output has no figure for is ``null``. Percentages are held as fractions in
this portal's Output and written as percent points.
"""

from __future__ import annotations

import csv
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import openpyxl

LAYOUT = Path(__file__).resolve().parent.parent / "config" / "webforms" / "webform.csv"
TEN = Decimal("1E-10")
# this portal's Output holds percentages as fractions (output_pct_scale 1); the webform takes percent points
PCT_FACTOR = Decimal(100)
# rows the webform repeats from another par_id (identical in the filled webforms)
MIRROR = {2813: 1380, 2814: 3136}
# par ids the Output uses for a different figure than the webform row of the same id:
# 4257 is the platform's Gross Transactions in the Output, but 'BPC (Mn)' in the webform
NOT_HERE = {4257}
HEAD = ["parent_question", "parameter_name", "par_id", "unit"]


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).lower())


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v).strip())
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _sheet(wb):
    name = next((n for n in wb.sheetnames if n.endswith("-WF")), None) or wb.sheetnames[0]
    return wb[name]


def output_values(output_path) -> dict[int, Decimal]:
    """par_id -> value from the Output workbook's upload sheet (first figure wins when a par repeats)."""
    wb = openpyxl.load_workbook(output_path, data_only=True, read_only=True)
    out: dict[int, Decimal] = {}
    for r in _sheet(wb).iter_rows(values_only=True):
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


def output_label(output_path) -> tuple[str, str]:
    """(player, month label) from the Output, e.g. "Myntra — Oct'26" -> ("Myntra", "Oct'26")."""
    wb = openpyxl.load_workbook(output_path, read_only=True)
    head = next(_sheet(wb).iter_rows(min_row=1, max_row=1, values_only=True))
    month = str(head[4] or "").strip() if len(head) > 4 else ""
    title = ""
    if "Contents" in wb.sheetnames:
        title = str(next(wb["Contents"].iter_rows(min_row=1, max_row=1, values_only=True))[0] or "")
    wb.close()
    return title.split(" — ")[0].strip(), month


def _read_rows(path) -> list[list[str]]:
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        ws = openpyxl.load_workbook(p, data_only=True).worksheets[0]
        return [["" if c is None else str(c) for c in r[:5]] for r in ws.iter_rows(values_only=True)]
    with open(p, newline="", encoding="utf-8-sig") as f:
        return list(csv.reader(f))


def _checked(path) -> list[list[str]]:
    rows = _read_rows(path)
    if not rows or [h.strip() for h in rows[0][:4]] != HEAD:
        raise ValueError("The webform must have the columns parent_question, parameter_name, par_id, unit, Month.")
    return rows


def saved_layout(player: str | None = None) -> Path:
    """The one webform layout every Fashion player shares (Myntra's, as shared)."""
    return LAYOUT


def fill_webform(output_path, webform_path, out_path) -> dict:
    """Write the filled webform CSV to ``out_path``; return how many rows were filled or left null."""
    vals = output_values(output_path)
    rows = _checked(webform_path)
    filled = nulls = 0
    out = [rows[0][:5]]
    for r in rows[1:]:
        r = (r + [""] * 5)[:5]
        try:
            par = int(r[2])
            d = None if par in NOT_HERE else vals.get(par)
            if d is None and par in MIRROR:
                d = vals.get(MIRROR[par])
        except ValueError:
            d = None
        if d is None:
            r[4] = "null"
            nulls += 1
        else:
            if r[3].strip() == "%":
                d = d * PCT_FACTOR
            r[4] = str(d.quantize(TEN))
            filled += 1
        out.append(r)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f, lineterminator="\r\n").writerows(out)
    return {"rows": len(out) - 1, "filled": filled, "null": nulls, "path": str(out_path)}
