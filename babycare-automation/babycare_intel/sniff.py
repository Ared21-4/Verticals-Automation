"""Recognise an uploaded file by what is in it, not by what it is called.

A MIS saved as "Nykaa bpc sep'26.xlsx" says nothing of MIS in its name, and a file
named "MIS" may belong to another player. Contents are the reliable signal.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl

from .reference_store import load_player_list


def _norm(v: object) -> str:
    return " ".join(str(v).strip().lower().split()) if v is not None else ""


def sniff_file(path: str | Path) -> str:
    """One of: reference, mis, transcript, other."""
    p = Path(path)
    ext = p.suffix.lower()
    if any(t in p.stem.lower() for t in ("secondary", "secodanry", "secondry")):
        return "secondary"        # news and developments, not a call or a MIS
    if ext == ".docx" and any(t in p.stem.lower() for t in
                              ("last call", "prior call", "previous call", "last known", "old call")):
        return "prior_call"
    if ext == ".docx":
        return "transcript"
    if ext not in (".xlsx", ".xlsm"):
        return "other"
    try:
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    except Exception:
        return "other"
    try:
        cfg = load_player_list()
        cfg_players = {x["sheet"] for x in cfg["players"]}
        known = cfg_players | set(cfg.get("templates", []))
        # A full workbook holds many players; a single player's own workbook holds one (Snapdeal,
        # Tata CLiQ). Either is a reference: both carry the player sheet by its configured name.
        def _model_sheet(name: str) -> bool:      # the header row names a Par_ID / Parameter column
            from .readers.reference import find_header_row
            rows = [list(r) for r in wb[name].iter_rows(min_row=1, max_row=8, max_col=12,
                                                        values_only=True)]
            return find_header_row(rows) is not None

        if len(known & set(wb.sheetnames)) >= 3 or any(
                _model_sheet(n) for n in cfg_players & set(wb.sheetnames)):
            return "reference"
        ws = wb[wb.sheetnames[0]]
        first = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
        head = [_norm(c) for c in first[:3]]
        if "section" in head and any("metric" in h for h in head):
            return "mis"
        return "other"
    except Exception:
        return "other"
    finally:
        wb.close()
