"""Reads the player's reference workbook: the model sheet and the output template.

The reference workbook is the specification. Nothing here invents structure;
every row and column position comes from the player config, and anything the
workbook does not contain is reported as absent rather than filled in.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

import openpyxl

from ..domain import month_key, month_label


@dataclass
class ParameterRow:
    """One row of the output template (`*-WF`)."""

    parent_question: str
    parameter_name: str
    par_id: int
    unit: str
    row: int
    # True for a parameter the player configuration adds because the template
    # omits it. The template's own rows are never altered.
    added: bool = False


@dataclass
class ReferenceModel:
    """The parsed model sheet: a par_id/row indexed monthly history."""

    player: str
    model_sheet: str
    months: list[str]                                 # canonical keys, ordered
    month_cols: dict[str, int]                        # month key -> column index
    row_names: dict[int, str]                         # row -> metric label
    row_units: dict[int, str]
    par_to_row: dict[int, int]
    row_to_par: dict[int, int]
    series: dict[int, dict[str, float]]               # row -> {month: value}
    formulas: dict[int, dict[str, str]]               # row -> {month: formula}
    template: list[ParameterRow] = field(default_factory=list)
    calibration: dict[str, dict[str, float]] = field(default_factory=dict)

    # ---------------------------------------------------------------- lookups

    def value(self, row: int, month: str) -> float | None:
        return self.series.get(row, {}).get(month)

    def value_by_par(self, par_id: int, month: str) -> float | None:
        row = self.par_to_row.get(par_id)
        return None if row is None else self.value(row, month)

    def history(self, row: int, upto: str, limit: int | None = None) -> list[tuple[str, float]]:
        """Values strictly before `upto`, oldest first. Never leaks the target month."""
        out = [(m, v) for m in self.months if m < upto
               and (v := self.series.get(row, {}).get(m)) is not None]
        return out[-limit:] if limit else out

    def has_month(self, month: str) -> bool:
        return month in self.month_cols

    def last_actual_month(self) -> str | None:
        return self.months[-1] if self.months else None


_MONTH_ABBR = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _parse_month_header(v: Any) -> str | None:
    """Accept both real dates and the workbook's "Sep'26" text headers.

    Returns None for analysis columns such as "Sep MoM" or "Sep YoY", which sit
    to the right of the real months and must never be mistaken for data.
    """
    if isinstance(v, (_dt.datetime, _dt.date)):
        return month_key(v)
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or "mom" in s.lower() or "yoy" in s.lower():
        return None
    # Month headers are often annotated with the sale running that month, e.g.
    # "Dec'22 (Big Bold Sale)". Drop the note and keep the month, or those
    # columns are silently lost from the player's history.
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
    m = re.match(r"^([A-Za-z]{3,9})\s*['’]\s*(\d{2,4})$", s) \
        or re.match(r"^([A-Za-z]{3,9})\s+(\d{2})\s*['’]?$", s)     # "Sep 24'" in some BPC sheets
    if m:
        idx = _MONTH_ABBR.get(m.group(1)[:3].lower())
        if idx:
            yr = int(m.group(2))
            return f"{2000 + yr if yr < 100 else yr:04d}-{idx:02d}"
    return None


def _num(v: Any) -> float | None:
    """A number, also from a number typed as text ("1,625"), which some sheets hold."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if s and s[-1] != "%":
            try:
                return float(s)
            except ValueError:
                return None
    return None


def find_header_row(rows: list) -> int | None:
    """1-based index of the header row in the first rows of a player sheet.

    Most sheets head column A "par_id". Some BPC sheets leave that cell blank (or hold a stray
    character), so a row that heads a "Parameter" column and carries month headers counts too.
    """
    for i, row in enumerate(rows, start=1):
        if not row:
            continue
        if row[0] is not None and str(row[0]).strip().lower() == "par_id":
            return i
    for i, row in enumerate(rows, start=1):
        if not row:
            continue
        heads = [str(c).strip().lower() for c in row[:5] if c is not None]
        if "parameter" in heads and any(_parse_month_header(c) for c in row):
            return i
    # Babycare sheets: the months sit on one row and "Parameter" / "Units" on the row below.
    for i, row in enumerate(rows[:-1], start=1):
        if not row or sum(1 for c in row if _parse_month_header(c)) < 3:
            continue
        nxt = rows[i]
        if nxt and "parameter" in [str(c).strip().lower() for c in nxt[:5] if c is not None]:
            return i
    return None


_LABEL_PARS: dict[str, int] | None = None


def label_pars() -> dict[str, int]:
    """Row label -> par_id, for the rows BPC sheets leave without one (Shipped GMV, ...)."""
    global _LABEL_PARS
    if _LABEL_PARS is None:
        import json
        p = Path(__file__).resolve().parent.parent / "config" / "label_pars.json"
        raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        _LABEL_PARS = {re.sub(r"\s+", " ", k.strip().lower()): int(v)
                       for k, v in raw.items() if not k.startswith("_")}
    return _LABEL_PARS


_UNIT_WORDS = {"mn", "cr", "%", "#", "inr", "inr (cr.)", "days", "% of gmv", "units"}


def _norm_label(name: Any) -> str:
    """Lower-case, single-spaced label; a mangled accent ("Home D�cor") reads as 'e'."""
    s = str(name).replace("�", "e").replace("é", "e").replace("É", "e")
    return re.sub(r"\s+", " ", s.strip().lower())


def label_fallback(name: Any, unit: Any) -> Any:
    """The row's label. Helper rows below the main block keep their label in the units column."""
    if name is not None and str(name).strip():
        return name
    if isinstance(unit, str) and unit.strip() and unit.strip().lower() not in _UNIT_WORDS:
        return unit
    return None


_FORCED_RAW: dict | None = None


def _forced_raw() -> dict:
    global _FORCED_RAW
    if _FORCED_RAW is None:
        import json
        p = Path(__file__).resolve().parent.parent / "config" / "forced_pars.json"
        _FORCED_RAW = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return _FORCED_RAW


def _forced_rules(rows: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for k, v in rows.items():
        if k.startswith("_"):
            continue
        sec, _, lab = k.rpartition("::") if "::" in k else ("", "", k)
        out[f"{_norm_label(sec)}::{_norm_label(lab)}"] = int(v)
    return out


def forced_pars(sheet: str | None = None) -> tuple[dict[str, int], list[tuple[str, str]]]:
    """('section::label' -> par_id, section openers) for rows whose par_id is set by rule.

    Rules in `rows` apply to every sheet; those under `sheets[<sheet name>]` to that sheet only.
    """
    raw = _forced_raw()
    rules = _forced_rules(raw.get("rows") or {})
    if sheet:
        rules.update(_forced_rules((raw.get("sheets") or {}).get(sheet, {})))
    sections = [(k, v) for k, v in (raw.get("sections") or {}).items() if not k.startswith("_")]
    return rules, sections


def assign_pars(entries: list[tuple[int, Any, Any]], sheet: str | None = None) -> dict[int, int]:
    """row -> par_id for a sheet, from (row, name, raw par_id) entries in sheet order.

    Rows in `forced_pars.json` come first and replace whatever the sheet wrote: the two
    babycare sheets put the same par_id on different rows (the category-share par_ids sit on
    the GMV rows in one and on the share rows in the other). A written par_id wins over the
    rest. A row with none takes the par_id its label is known by, but only the first such row,
    and only if no other row already holds that par_id, so a repeated label further down
    (a category block's "Others") is never mistaken for the headline metric.
    """
    out: dict[int, int] = {}
    used: set[int] = set()
    forced, sections = forced_pars(sheet)
    if forced:
        section = ""
        for r, name, _raw in entries:
            if name is None:
                continue
            n = _norm_label(name)
            for sec, prefix in sections:
                if n.startswith(prefix):
                    section = sec
                    break
            pid = forced.get(f"{section}::{n}") or forced.get(f"::{n}")
            if pid is not None and pid not in used:
                out[r] = pid
                used.add(pid)
    forced_ids = set(used)
    for r, _name, raw in entries:
        if r in out:
            continue
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        if pid in forced_ids:           # that par_id is bound to its forced row
            continue
        out[r] = pid
        used.add(pid)
    aliases = label_pars()
    for r, name, raw in entries:
        if r in out or name is None:
            continue
        pid = aliases.get(re.sub(r"\s+", " ", str(name).strip().lower()))
        if pid is not None and pid not in used:
            out[r] = pid
            used.add(pid)
    return out


def load_reference(path: str, cfg: dict) -> ReferenceModel:
    """Parse the model sheet and output template out of the reference workbook."""
    ref = cfg["reference"]
    wb_v = openpyxl.load_workbook(path, data_only=True, read_only=True)
    wb_f = openpyxl.load_workbook(path, data_only=False, read_only=True)

    sheet = ref["model_sheet"]
    if sheet not in wb_v.sheetnames:
        raise ValueError(
            f"Reference workbook has no sheet '{sheet}'. "
            f"Found {len(wb_v.sheetnames)} sheets; check the player configuration."
        )

    ws_v, ws_f = wb_v[sheet], wb_f[sheet]
    grid_v = [list(r) for r in ws_v.iter_rows(
        min_row=1, max_row=ref["last_data_row"], values_only=True)]
    grid_f = [list(r) for r in ws_f.iter_rows(
        min_row=1, max_row=ref["last_data_row"], values_only=True)]

    def cell(grid, r: int, c: int):
        row = grid[r - 1] if 0 < r <= len(grid) else None
        return row[c - 1] if row and 0 < c <= len(row) else None

    # Player sheets head their label column "Parameter" and units "Units"; the layout moves by
    # a column between players, so read it from the header rather than assume it.
    hdr_row = ref["header_row"]
    heads = {str(c).strip().lower(): i + 1 for i, c in enumerate(grid_v[hdr_row - 1] if hdr_row <= len(grid_v) else [])
             if c is not None}
    if "parameter" in heads:
        ref = dict(ref, name_col=heads["parameter"], unit_col=heads.get("units", ref["unit_col"]))
    # Some player workbooks (Snapdeal, Tata CLiQ) put months straight after the name, with no
    # units column. Column 3 is then a month, not a unit, so units come from the name instead.
    no_units = ref["unit_col"] >= ref["first_month_col"] or (
        "units" not in heads and "parameter" not in heads and ref["first_month_col"] <= 3)

    # ---- month axis -------------------------------------------------------
    month_cols: dict[str, int] = {}
    width = max(len(r) for r in grid_v)
    for c in range(ref["first_month_col"], width + 1):
        key = _parse_month_header(cell(grid_v, hdr_row, c))
        if key and key not in month_cols:
            month_cols[key] = c
    months = sorted(month_cols)

    # ---- metric rows ------------------------------------------------------
    row_names, row_units, par_to_row, row_to_par = {}, {}, {}, {}
    series: dict[int, dict[str, float]] = {}
    formulas: dict[int, dict[str, str]] = {}

    rows_range = range(ref["first_data_row"], ref["last_data_row"] + 1)
    pars_by_row = assign_pars([(r, label_fallback(cell(grid_v, r, ref["name_col"]),
                                                  cell(grid_v, r, ref["unit_col"])),
                                cell(grid_v, r, ref["par_id_col"])) for r in rows_range],
                               sheet=sheet)

    for r in rows_range:
        name = cell(grid_v, r, ref["name_col"])
        if name is not None:
            row_names[r] = str(name).strip()
        unit = None if no_units else cell(grid_v, r, ref["unit_col"])
        if unit is None and no_units and name is not None:
            mu = re.search(r"\(([^()]{1,12})\)\s*$", str(name))
            unit = mu.group(1) if mu else None
        if unit is not None:
            row_units[r] = str(unit).strip()
        pid = pars_by_row.get(r)
        vals, fmls = {}, {}
        for m, c in month_cols.items():
            v = _num(cell(grid_v, r, c))
            if v is not None:
                vals[m] = v
            f = cell(grid_f, r, c)
            if isinstance(f, str) and f.startswith("="):
                fmls[m] = f
        if vals:
            series[r] = vals
        if fmls:
            formulas[r] = fmls
        if pid is not None:
            # First row wins, unless it holds no data at all and this one does (a sheet that
            # keeps a par_id on an empty row and the figures on a helper row below).
            held = par_to_row.get(pid)
            if held is None or (held not in series and r in series):
                par_to_row[pid] = r
            row_to_par[r] = pid

    # ---- the existing RSC-vs-expert calibration block ---------------------
    cb = ref.get("calibration_block", {})
    calibration: dict[str, dict[str, float]] = {}
    for label, key in (("rsc_yoy_row", "rsc_yoy"), ("expert_yoy_row", "expert_yoy"),
                       ("expert_number_row", "expert_number"), ("deviation_row", "deviation")):
        r = cb.get(label)
        if not r:
            continue
        got = {}
        for m, c in month_cols.items():
            v = cell(grid_v, r, c)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                got[m] = float(v)
        calibration[key] = got

    # ---- output template --------------------------------------------------
    template: list[ParameterRow] = []
    tpl_name = ref.get("output_template_sheet")
    ws_t, wb_t = None, None
    tpl_file = ref.get("output_template_file")
    if tpl_file:
        # The delivered BPC upload sheet is fixed (178 rows) and the same for every player, so it
        # ships with the portal instead of being read from whichever workbook is uploaded.
        import json
        rows = json.loads((Path(__file__).resolve().parent.parent / "config" / tpl_file)
                          .read_text(encoding="utf-8"))["rows"]
        for i, (q, nm, pid, unit) in enumerate(rows, start=2):
            template.append(ParameterRow(parent_question=q, parameter_name=nm,
                                         par_id=int(pid), unit=unit, row=i))
    elif tpl_name and tpl_name in wb_v.sheetnames:
        ws_t = wb_v[tpl_name]
    elif tpl_name:
        # A workbook holding one player (Snapdeal, Tata CLiQ) carries no output template, so the
        # built-in one is used: every player is delivered in the same layout.
        from ..reference_store import default_reference
        built = default_reference()
        if built and Path(built).exists() and Path(built).resolve() != Path(path).resolve():
            wb_t = openpyxl.load_workbook(built, data_only=True, read_only=True)
            if tpl_name in wb_t.sheetnames:
                ws_t = wb_t[tpl_name]
    if ws_t is not None:
        last_q = ""
        for i, row in enumerate(ws_t.iter_rows(min_row=2, values_only=True), start=2):
            if not row or len(row) < 4 or row[2] is None:
                continue
            try:
                pid = int(row[2])
            except (TypeError, ValueError):
                continue
            q = (str(row[0]).strip() if row[0] else "") or last_q
            last_q = q
            template.append(ParameterRow(
                parent_question=q,
                parameter_name=str(row[1]).strip() if row[1] else "",
                par_id=pid,
                unit=str(row[3]).strip() if row[3] else "",
                row=i,
            ))

    # Parameters the model holds but the template omits, declared per player.
    existing = {t.par_id for t in template}
    for spec in (cfg.get("extra_parameters") or {}).get("rows", []):
        pid = int(spec["par_id"])
        if pid in existing:
            continue          # never shadow a template row
        # Added whether or not this player's sheet carries the metric: where it
        # does not, the engine derives it (category share x shipped GMV) or the
        # row is exported as null. Nothing is invented either way.
        template.append(ParameterRow(
            parent_question=spec.get("parent_question", ""),
            parameter_name=spec.get("parameter_name", ""),
            par_id=pid,
            unit=spec.get("unit", row_units.get(par_to_row.get(pid, -1), "")),
            row=-1,
            added=True,
        ))
        existing.add(pid)

    wb_v.close()
    wb_f.close()
    if wb_t is not None:
        wb_t.close()
    return ReferenceModel(
        player=cfg["player"], model_sheet=sheet, months=months, month_cols=month_cols,
        row_names=row_names, row_units=row_units, par_to_row=par_to_row,
        row_to_par=row_to_par, series=series, formulas=formulas,
        template=template, calibration=calibration,
    )


def describe(model: ReferenceModel) -> dict[str, Any]:
    """Short summary used by the validation screen."""
    return {
        "model_sheet": model.model_sheet,
        "months": len(model.months),
        "first_month": month_label(model.months[0]) if model.months else None,
        "last_month": month_label(model.months[-1]) if model.months else None,
        "metric_rows": len(model.row_names),
        "par_ids": len(model.par_to_row),
        "template_rows": len(model.template),
        "template_rows_from_workbook": sum(1 for t in model.template if not t.added),
        "template_rows_added_by_config": sum(1 for t in model.template if t.added),
        "template_par_ids_in_model": sum(
            1 for t in model.template if t.par_id in model.par_to_row),
    }
