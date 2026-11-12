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
    # True for a row of the player's sheet that is shown in the Analysis only: it is not part of the
    # upload layout, so the Output sheet leaves it out.
    analysis_only: bool = False


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
    m = re.match(r"^([A-Za-z]{3,9})\s*['’]\s*(\d{2,4})$", s)
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

    for r in range(ref["first_data_row"], ref["last_data_row"] + 1):
        name = cell(grid_v, r, ref["name_col"])
        if name is not None:
            row_names[r] = str(name).strip()
        unit = None if no_units else cell(grid_v, r, ref["unit_col"])
        if unit is None and no_units and name is not None:
            mu = re.search(r"\(([^()]{1,12})\)\s*$", str(name))
            unit = mu.group(1) if mu else None
        if unit is not None:
            row_units[r] = str(unit).strip()
        raw_par = cell(grid_v, r, ref["par_id_col"])
        if raw_par is not None:
            try:
                pid = int(raw_par)
            except (TypeError, ValueError):
                pid = None
            if pid is not None:
                par_to_row.setdefault(pid, r)
                row_to_par[r] = pid
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
    if tpl_name and tpl_name in wb_v.sheetnames:
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
    model = ReferenceModel(
        player=cfg["player"], model_sheet=sheet, months=months, month_cols=month_cols,
        row_names=row_names, row_units=row_units, par_to_row=par_to_row,
        row_to_par=row_to_par, series=series, formulas=formulas,
        template=template, calibration=calibration,
    )
    _extend_own_layout(model, cfg)
    return model



SYNTHETIC_BASE = 900000        # a sheet row with no par_id is addressed as SYNTHETIC_BASE + its row number
_NOT_A_METRIC = re.compile(r"check|deviation|reported|absolute|^total$|^sum$", re.I)


def _extend_own_layout(model: ReferenceModel, cfg: dict) -> None:
    """Deliver everything the sheet holds for a player with a webform of its own (Tata CLiQ, Snapdeal).

    1. Every row of that webform, which the shared template omits. The engine estimates those the sheet
       carries from its history; the rest are exported as null, so the Output has a line item per webform row.
    2. Every other row of the sheet with figures in the last year (L1, L2 and L3 GMV, ASP, units, AOV,
       users, frequency ...), for the Analysis only. Rows the sheet leaves without a par_id are addressed
       by their sheet row (SYNTHETIC_BASE + row); the sheet's own check and total rows are left out.
    """
    from ..engine.itemblocks import item_blocks
    from ..export.webform import OWN_LAYOUT, WEBFORMS, slug
    own = OWN_LAYOUT.get(slug(cfg.get("display_name") or cfg.get("player") or ""))
    if not own or not (WEBFORMS / own).exists():
        return
    import csv as _csv
    existing = {t.par_id for t in model.template}
    with open(WEBFORMS / own, newline="", encoding="utf-8") as f:
        for r_ in list(_csv.reader(f))[1:]:
            try:
                pid = int(r_[2])
            except (IndexError, ValueError):
                continue
            if pid in existing:
                continue
            model.template.append(ParameterRow(
                parent_question=r_[0], parameter_name=r_[1], par_id=pid,
                unit=r_[3] if len(r_) > 3 else "", row=-1, added=True))
            existing.add(pid)

    blk = item_blocks(model)                    # the L1 GMV / ASP / items blocks are added by the exporter already
    covered: set[int] = set()
    if blk:
        covered = {e[k] for e in blk["entries"] for k in ("grow", "arow", "irow")} | {blk["total"]["row"]}
    recent = model.months[-12:]
    header, header_unit = "", ""
    for r in sorted(model.row_names):
        name = model.row_names[r]
        vals = model.series.get(r, {})
        par = model.row_to_par.get(r)
        if par is None and len(vals) <= 4 and name:          # a section heading
            header = name
            mu = re.search(r"\(([^()]{1,12})\)\s*$", name)
            header_unit = mu.group(1) if mu else ""
            continue
        if r in covered or not any(m in vals for m in recent) or _NOT_A_METRIC.search(name):
            continue
        if par is None:
            par = SYNTHETIC_BASE + r
            model.par_to_row[par] = r
            model.row_to_par[r] = par
        if par in existing:
            continue
        model.template.append(ParameterRow(
            parent_question=header, parameter_name=name, par_id=par,
            unit=model.row_units.get(r) or header_unit, row=-1, added=True, analysis_only=True))
        existing.add(par)

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
