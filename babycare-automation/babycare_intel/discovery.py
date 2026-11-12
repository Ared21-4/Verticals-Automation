"""Finds the players a reference workbook contains.

Every player sheet in the workbook follows the same shape: `par_id` in column A,
a header row that carries the month axis, and one row per metric. That is enough
to discover players automatically, so adding a player to the workbook is enough
to make it available in the portal.

Nothing here guesses at a player's meaning. It reports what a sheet contains —
how many metrics, which months, whether the core topline is present — and lets
the caller decide.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any

import openpyxl

from .domain import month_label
from .readers.reference import _parse_month_header, assign_pars, find_header_row, label_fallback

# Sheets that are structurally player-like but are not players.
_NON_PLAYER = re.compile(
    r"^(000|Sheet\d|rough|ROUGH|RDRFEBGBD|Working|List|New List|BPC - List|"
    r"Deck calculations|Quarterly GMV|FashDash|.*_WF|.*----->|.*ASP Cuts|"
    r"Ajio Query|AOV D2C|Sectorwide_BPC|Check.*)$", re.I)

# Superseded or scratch copies. Kept out of the default list but still openable.
# "revised" is deliberately absent: a revised sheet is the newer one, not a stale one.
_STALE = re.compile(r"(_OLD|DON'T\s*USE|\(\s*\d+\s*\)|Rough)", re.I)

# Suffixes the workbook uses to version a player's sheet. Stripped so that every
# variant of a player collapses onto one display name.
_SUFFIX = re.compile(
    r"(_AJ|_VR|_WF|_OLD|\s*\(\s*DON'T\s*USE\s*\)|\s*\(\s*\d+\s*\)|"
    r"\s*\(\s*Rough\s*\)|\s+revised)\s*$", re.I)

# The topline a run needs before it can produce anything meaningful.
CORE_PARS = {241: "Gross GMV", 242: "Cancellation %", 240: "AOV", 238: "MTU"}


@dataclass
class PlayerSheet:
    sheet: str
    header_row: int
    first_month_col: int
    par_ids: dict[int, int]            # par_id -> row
    months: list[str]
    bad_month_headers: list[str] = field(default_factory=list)
    stale: bool = False
    display: str = ""

    @property
    def name(self) -> str:
        """Display name: the configured one, else the sheet name without version suffixes."""
        if self.display:
            return self.display
        n = self.sheet.strip()
        for _ in range(4):
            stripped = _SUFFIX.sub("", n).strip()
            if stripped == n:
                break
            n = stripped
        return n or self.sheet.strip()

    @property
    def n_metrics(self) -> int:
        return len(self.par_ids)

    @property
    def latest_month(self) -> str | None:
        return self.months[-1] if self.months else None

    @property
    def missing_core(self) -> list[str]:
        return [lab for pid, lab in CORE_PARS.items() if pid not in self.par_ids]

    @property
    def usable(self) -> bool:
        return not self.missing_core and len(self.months) >= 13

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet": self.sheet, "name": self.name, "metrics": self.n_metrics,
            "months": len(self.months),
            "first_month": month_label(self.months[0]) if self.months else None,
            "latest_month": month_label(self.latest_month) if self.latest_month else None,
            "usable": self.usable,
            "missing_core": ", ".join(self.missing_core),
            "stale": self.stale,
        }


def _allowlist() -> dict[str, str]:
    """sheet -> display name for the players this system covers."""
    from .reference_store import load_player_list
    return {p["sheet"]: p["name"] for p in load_player_list()["players"]}


def _start_months() -> dict[str, str]:
    """sheet -> first month ('YYYY-MM') the player's history is read from, where one is set.

    Revised Babycare starts at Jan'21, so Firstcry reads from its first column.
    """
    from .reference_store import load_player_list
    return {p["sheet"]: p["start_month"] for p in load_player_list()["players"]
            if p.get("start_month")}


_CACHE: dict[tuple, list] = {}


def discover_players(path: str, include_stale: bool = False,
                     max_header_row: int = 12, restrict: bool = True) -> list[PlayerSheet]:
    """The player sheets a reference workbook holds.

    With `restrict` (the default) only the babycare players listed in
    config/babycare_players.json are returned, under their configured names.
    """
    import os
    try:
        key = (os.path.abspath(path), os.path.getmtime(path), include_stale, restrict)
    except OSError:
        key = None
    if key and key in _CACHE:
        return _CACHE[key]
    result = _discover(path, include_stale, max_header_row, restrict)
    if key:
        _CACHE[key] = result
    return result


def _discover(path: str, include_stale: bool, max_header_row: int,
              restrict: bool) -> list[PlayerSheet]:
    allow = _allowlist() if restrict else None
    starts = _start_months() if restrict else {}
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out: list[PlayerSheet] = []

    for name in wb.sheetnames:
        if allow is not None:
            if name not in allow:
                continue
        elif _NON_PLAYER.match(name.strip()):
            continue
        ws = wb[name]
        try:
            head_block = [list(r) for r in ws.iter_rows(
                min_row=1, max_row=max_header_row, values_only=True)]
        except Exception:
            continue
        if not head_block:
            continue

        hdr = find_header_row(head_block)
        if hdr is None:
            continue
        name_col = next((c for c, v in enumerate(head_block[hdr - 1])
                         if v is not None and str(v).strip().lower() == "parameter"), 1)

        header = head_block[hdr - 1]
        months, bad, first_col = [], [], None
        for c in range(1, len(header) + 1):
            raw = header[c - 1]
            if raw is None:
                continue
            key = _parse_month_header(raw)
            if key and key < starts.get(name, ""):
                continue
            if key:
                months.append(key)
                if first_col is None:
                    first_col = c
            elif c >= 4 and isinstance(raw, str) and raw.strip() and len(bad) < 8:
                # e.g. "May'24 (Pink Summer Sale)" — a month header with a note.
                if re.search(r"\d{2}", raw):
                    bad.append(raw.strip())

        entries = []
        for i, row in enumerate(ws.iter_rows(min_row=hdr + 1, values_only=True),
                                start=hdr + 1):
            if not row:
                continue
            label = label_fallback(row[name_col] if len(row) > name_col else None,
                                   row[name_col + 1] if len(row) > name_col + 1 else None)
            entries.append((i, label, row[0]))
        par_ids: dict[int, int] = {}
        for r, pid in assign_pars(entries, sheet=name).items():
            par_ids.setdefault(pid, r)

        if not par_ids:
            continue

        out.append(PlayerSheet(
            sheet=name, header_row=hdr, first_month_col=first_col or 4,
            par_ids=par_ids, months=sorted(set(months)), bad_month_headers=bad,
            stale=bool(_STALE.search(name)) if allow is None else False,
            display=(allow or {}).get(name, ""),
        ))

    wb.close()
    if not include_stale:
        # Where several sheets share a display name, keep the richest current one.
        best: dict[str, PlayerSheet] = {}
        for p in out:
            cur = best.get(p.name.lower())
            if cur is None:
                best[p.name.lower()] = p
                continue
            key = (not p.stale, p.latest_month or "", p.n_metrics)
            cur_key = (not cur.stale, cur.latest_month or "", cur.n_metrics)
            if key > cur_key:
                best[p.name.lower()] = p
        out = list(best.values())

    return sorted(out, key=lambda p: (not p.usable, p.name.lower()))


def summarise(players: list[PlayerSheet]) -> list[dict[str, Any]]:
    return [p.to_dict() for p in players]
