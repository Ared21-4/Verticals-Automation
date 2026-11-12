"""The built-in reference workbook.

The full master workbook is large and mostly irrelevant here (BPC, scratch and
superseded tabs). This module slims it to the BPC players' history plus the
output templates and keeps that copy inside the project, so nobody uploads a
reference every month.

The slim copy holds values only. Refreshing it from a newer master workbook is
one call (`build_slim_reference`) or one button in the portal.
"""

from __future__ import annotations

import json
from pathlib import Path

import openpyxl
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parent.parent
BUILTIN = ROOT / "reference" / "BPC Reference.xlsx"
PLAYERS_CFG = Path(__file__).parent / "config" / "bpc_players.json"


def load_player_list() -> dict:
    return json.loads(PLAYERS_CFG.read_text(encoding="utf-8"))


def default_reference() -> str | None:
    """Path of the built-in reference, or None if it has not been built yet."""
    return str(BUILTIN) if BUILTIN.exists() else None


def resolve_reference(path: str | None) -> str:
    """The workbook to use: the one given, else the built-in copy."""
    if path:
        return path
    built = default_reference()
    if built:
        return built
    raise FileNotFoundError(
        "No reference workbook. The built-in copy is missing "
        f"({BUILTIN}); build it with build_slim_reference(<master workbook>) or "
        "upload the master workbook in the portal.")


def build_slim_reference(src: str, dst: str | Path = BUILTIN) -> dict:
    """Copy the BPC players' sheets and the output templates, values only."""
    cfg = load_player_list()
    wanted = [p["sheet"] for p in cfg["players"]] + list(cfg.get("templates", []))

    wb_in = openpyxl.load_workbook(src, data_only=True, read_only=True)
    missing = [s for s in wanted if s not in wb_in.sheetnames]
    # Players added from their own workbooks (add_player_sheet) are not in a master workbook;
    # keep them rather than lose them on a refresh.
    keep = {}
    if Path(dst).exists():
        old = openpyxl.load_workbook(dst, data_only=True, read_only=True)
        for name in missing:
            if name in old.sheetnames:
                keep[name] = [list(r) for r in old[name].iter_rows(values_only=True)]
        old.close()
    out = Workbook()
    out.remove(out.active)
    copied = []
    for name in wanted:
        if name not in wb_in.sheetnames:
            if name in keep:
                ws_out = out.create_sheet(name)
                for row in keep[name]:
                    ws_out.append(row)
                copied.append(name)
            continue
        ws_out = out.create_sheet(name)
        for row in wb_in[name].iter_rows(values_only=True):
            ws_out.append(list(row))
        copied.append(name)
    wb_in.close()
    missing = [m for m in missing if m not in keep]

    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst)
    return {"path": str(dst), "copied": copied, "missing": missing,
            "size_kb": round(dst.stat().st_size / 1024, 1)}


def add_player_sheet(src: str, sheet: str, dst: str | Path = BUILTIN, as_name: str | None = None) -> dict:
    """Add one player from that player's own workbook (values only) to the built-in reference.

    Only the values block is kept: the columns up to the last month of the first month run, so
    the contribution and growth blocks that sit to its right are left behind.
    """
    from .readers.reference import _parse_month_header
    dst = Path(dst)
    wb_in = openpyxl.load_workbook(src, data_only=True, read_only=True)
    if sheet not in wb_in.sheetnames:
        raise ValueError(f"'{sheet}' is not in the workbook (found {wb_in.sheetnames[:6]}...).")
    rows = [list(r) for r in wb_in[sheet].iter_rows(values_only=True)]
    wb_in.close()
    hdr = next((i for i, r in enumerate(rows[:12]) if r and r[0] is not None
                and str(r[0]).strip().lower() == "par_id"), None)
    if hdr is None:
        raise ValueError(f"'{sheet}' has no Par_ID header row.")
    last = 0
    for c, v in enumerate(rows[hdr], start=1):
        if _parse_month_header(v):
            last = c
        elif last and v is None:
            break                                   # the first month run has ended
    rows = [r[:last] for r in rows]

    if dst.exists():
        wb = openpyxl.load_workbook(dst, data_only=True)
    else:
        wb = Workbook()
        wb.remove(wb.active)
    name = as_name or sheet
    if name in wb.sheetnames:
        del wb[name]
    ws = wb.create_sheet(name)
    for r in rows:
        ws.append(r)
    dst.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dst)
    return {"sheet": name, "rows": len(rows), "columns": last, "path": str(dst)}


def workbook_has_player(path: str, player: str) -> bool:
    """True if the workbook holds this player's model sheet (by the configured sheet name)."""
    key = player.strip().lower()
    for p in load_player_list()["players"]:
        if key == p["name"].lower() or key in p.get("aliases", []) or key == p["sheet"].lower():
            sheet = p["sheet"]
            break
    else:
        return True                       # not a listed player: let the normal checks decide
    try:
        wb = openpyxl.load_workbook(path, read_only=True)
        ok = sheet in wb.sheetnames
        wb.close()
        return ok
    except Exception:
        return True                       # unreadable: the reader reports that properly

