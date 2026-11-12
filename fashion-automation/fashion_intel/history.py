"""Month-to-month history, kept without re-uploading a reference workbook.

The built-in reference ends at a fixed month. After a run, "save to history"
stores that month's figures beside it; the next run reads them as the prior
month. Saved values are marked as system estimates unless the run was anchored
on a MIS, and re-saving a month replaces it, so confirmed actuals can supersede
an earlier estimate.

Nothing in the reference workbook is edited. The overlay is applied in memory
when the reference is loaded.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

from .domain import Provenance, is_sheet_row


def _path(root: str | Path, key: str) -> Path:
    return Path(root) / "history" / f"{key}.json"


def load_overlay(root: str | Path, key: str) -> dict[str, Any]:
    p = _path(root, key)
    if not p.exists():
        return {"months": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"months": {}}


def overlay_months(root: str | Path, key: str) -> list[str]:
    return sorted(load_overlay(root, key).get("months", {}))


def apply_overlay(ref, root: str | Path, key: str, authoritative: bool = False) -> list[str]:
    """Merge saved months into a loaded reference. Returns notes for the audit trail."""
    notes: list[str] = []
    months = load_overlay(root, key).get("months", {})
    for month, block in sorted(months.items()):
        if authoritative and ref.month_cols.get(month, -1) > 0 and any(
                month in v for v in ref.series.values()):
            # An uploaded reference carries its own numbers for this month; they win.
            notes.append(f"History: {month} is already in the uploaded reference, so the "
                         "saved run for it was not applied.")
            continue
        applied = skipped = 0
        for row_s, rec in block.get("rows", {}).items():
            row = int(row_s)
            # A saved row is only trusted if the sheet still has the same metric there.
            if ref.row_names.get(row) != rec.get("name"):
                skipped += 1
                continue
            ref.series.setdefault(row, {})[month] = float(rec["v"])
            applied += 1
        if month not in ref.month_cols:
            ref.month_cols[month] = -1            # no sheet column; lives in the overlay
            ref.months = sorted(set(ref.months) | {month})
        notes.append(f"History: {month} restored from run {block.get('run_id', '?')} "
                     f"({applied} values{f', {skipped} skipped' if skipped else ''}).")
    return notes


def save_month(root: str | Path, key: str, month: str, est, ref, run_id: str,
               anchored_on_mis: bool, calibration: dict | None = None,
               basis: str | None = None) -> dict[str, Any]:
    """Store a run's figures as that month's history. Replaces any earlier save."""
    rows: dict[str, dict[str, Any]] = {}
    for row, v in est.rows.items():
        if not is_sheet_row(row) or v is None or v.is_null:
            continue
        name = ref.row_names.get(row)
        if not name:
            continue
        rows[str(row)] = {"v": v.value, "name": name, "prov": v.provenance.value}
    overlay = load_overlay(root, key)
    overlay.setdefault("months", {})[month] = {
        "run_id": run_id,
        "saved_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "basis": basis or ("mis_anchored" if anchored_on_mis else "model_estimate"),
        "unverified": True,
        "calibration": calibration or {},
        "rows": rows,
    }
    p = _path(root, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(overlay, indent=1), encoding="utf-8")
    return {"month": month, "values": len(rows), "path": str(p)}


def remove_month(root: str | Path, key: str, month: str) -> bool:
    overlay = load_overlay(root, key)
    if month not in overlay.get("months", {}):
        return False
    del overlay["months"][month]
    _path(root, key).write_text(json.dumps(overlay, indent=1), encoding="utf-8")
    return True


def describe(root: str | Path, key: str) -> list[dict[str, Any]]:
    out = []
    for m, b in sorted(load_overlay(root, key).get("months", {}).items()):
        out.append({"month": m, "values": len(b.get("rows", {})),
                    "basis": b.get("basis"), "saved_at": b.get("saved_at"),
                    "run_id": b.get("run_id")})
    return out


def calibration_records(root: str | Path, key: str, before: str) -> list[dict[str, Any]]:
    """Earlier saved months' balance records, oldest first. Only months before `before`."""
    out = []
    for m, b in sorted(load_overlay(root, key).get("months", {}).items()):
        if m < before and b.get("calibration"):
            out.append({"month": m, **b["calibration"]})
    return out
