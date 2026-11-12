"""Reads a player MIS workbook into sections of labelled values.

The MIS layout is: a forward-filled Section column, a Metric column, then one
column per reporting month. Blank cells stay blank — a missing metric is never
turned into a zero.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import openpyxl

from ..domain import month_key
from .reference import _parse_month_header

_NUM = re.compile(r"^\s*(-?[\d,]*\.?\d+)\s*(%|mn|m|bn|b|k|cr|crore)?\s*$", re.I)
_SUFFIX = {"mn": 1e6, "m": 1e6, "bn": 1e9, "b": 1e9, "k": 1e3}


@dataclass
class MisCell:
    section: str
    metric: str
    value: float | None
    raw: Any
    unit_hint: str = ""
    address: str = ""
    mom: float | None = None      # growth the MIS states beside the value, if it has MoM/YoY columns
    yoy: float | None = None

    @property
    def missing(self) -> bool:
        return self.value is None


@dataclass
class MisData:
    path: str
    sheet: str
    months: list[str]
    month_cols: dict[str, int]
    # month -> (section, metric) -> cell. Sections are part of the key because
    # a MIS routinely repeats the same metric names under different sections
    # (Myntra lists the same 17 categories under both GMV and ASP).
    cells: dict[str, dict[tuple[str, str], MisCell]] = field(default_factory=dict)
    sections: dict[str, list[str]] = field(default_factory=dict)
    parse_notes: list[str] = field(default_factory=list)

    def get(self, month: str, metric: str, section: str | None = None) -> MisCell | None:
        month_cells = self.cells.get(month, {})
        if section is not None:
            return month_cells.get((section, metric))
        hits = [c for (s, m), c in month_cells.items() if m == metric]
        if len(hits) > 1:
            raise KeyError(
                f"Metric '{metric}' appears in {len(hits)} MIS sections "
                f"({', '.join(sorted(c.section for c in hits))}); pass section= to disambiguate."
            )
        return hits[0] if hits else None

    def value(self, month: str, metric: str, section: str | None = None) -> float | None:
        c = self.get(month, metric, section)
        return None if c is None else c.value

    def section_values(self, month: str, section: str) -> dict[str, float]:
        """Non-missing values of one section, keyed by metric name."""
        return {
            m: c.value
            for (s, m), c in self.cells.get(month, {}).items()
            if s == section and c.value is not None
        }

    def section_cells(self, month: str, section: str) -> dict[str, MisCell]:
        return {m: c for (s, m), c in self.cells.get(month, {}).items() if s == section}

    def missing_metrics(self, month: str) -> list[tuple[str, str]]:
        return [(s, m) for (s, m), c in self.cells.get(month, {}).items() if c.missing]


def parse_number(raw: Any) -> tuple[float | None, str]:
    """Return (value, unit_hint). Handles '80 M', '0.3', '34 M', '1,234 Cr'."""
    if raw is None:
        return None, ""
    if isinstance(raw, bool):
        return None, ""
    if isinstance(raw, (int, float)):
        return float(raw), ""
    s = str(raw).strip()
    if not s:
        return None, ""
    m = _NUM.match(s)
    if not m:
        return None, ""
    num = float(m.group(1).replace(",", ""))
    suf = (m.group(2) or "").lower()
    if suf == "%":
        return num / 100.0, "%"
    if suf in _SUFFIX:
        # Keep the natural unit of the sheet: counts expressed as "80 M" are
        # returned as 80 with a hint of Mn, not as 80,000,000.
        return num, {"mn": "Mn", "m": "Mn", "bn": "Bn", "b": "Bn", "k": "K"}[suf]
    if suf in ("cr", "crore"):
        return num, "INR Cr"
    return num, ""


def load_mis(path: str, sheet: str | None = None) -> MisData:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    name = sheet or wb.sheetnames[0]
    if name not in wb.sheetnames:
        raise ValueError(f"MIS workbook has no sheet '{name}'. Found: {wb.sheetnames}")
    ws = wb[name]
    grid = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()

    if not grid:
        raise ValueError(f"MIS sheet '{name}' is empty.")

    notes: list[str] = []
    header = grid[0]
    month_cols: dict[str, int] = {}
    for c in range(3, len(header) + 1):
        key = _parse_month_header(header[c - 1])
        if key:
            month_cols[key] = c
    # Optional "MoM" / "YoY" columns sit to the right of a month's value column.
    growth_cols: dict[str, dict[str, int]] = {}
    last_month = None
    for c in range(3, len(header) + 1):
        h = header[c - 1]
        key = _parse_month_header(h)
        if key:
            last_month = key
        elif last_month and isinstance(h, str) and h.strip().lower() in ("mom", "yoy"):
            growth_cols.setdefault(last_month, {})[h.strip().lower()] = c

    if not month_cols:
        raise ValueError(
            "No reporting-month column found in the MIS header row. "
            "Expected a header such as \"Sep'26\" from column C onward."
        )

    cells: dict[str, dict[tuple[str, str], MisCell]] = {m: {} for m in month_cols}
    sections: dict[str, list[str]] = {}
    section = ""
    for r_i, row in enumerate(grid[1:], start=2):
        if not row:
            continue
        a = row[0] if len(row) > 0 else None
        b = row[1] if len(row) > 1 else None
        if a is not None and str(a).strip():
            section = str(a).strip()
        metric = str(b).strip() if b is not None and str(b).strip() else None
        if not metric:
            continue
        sections.setdefault(section, []).append(metric)
        for m, c in month_cols.items():
            raw = row[c - 1] if len(row) >= c else None
            val, hint = parse_number(raw)
            if raw is not None and val is None:
                notes.append(f"Row {r_i} '{metric}': could not read value {raw!r}; treated as missing.")
            col_letter = openpyxl.utils.get_column_letter(c)
            gc = growth_cols.get(m, {})
            mom = yoy = None
            for name_, target in (("mom", "mom"), ("yoy", "yoy")):
                gcol = gc.get(name_)
                if gcol and len(row) >= gcol:
                    gv, _h = parse_number(row[gcol - 1])
                    if gv is not None:
                        if target == "mom":
                            mom = gv
                        else:
                            yoy = gv
            cells[m][(section, metric)] = MisCell(
                section=section, metric=metric, value=val, raw=raw,
                unit_hint=hint, address=f"{col_letter}{r_i}", mom=mom, yoy=yoy,
            )

    return MisData(
        path=path, sheet=name, months=sorted(month_cols), month_cols=month_cols,
        cells=cells, sections=sections, parse_notes=notes,
    )
