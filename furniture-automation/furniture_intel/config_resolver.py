"""Builds a player's working configuration.

Three layers, in order:

1. `base_furniture.json` â€” the shared profile, addressing metrics by par_id.
2. `players/<name>.json` â€” whatever that player genuinely does differently.
3. The player's own sheet â€” par_ids are resolved to row numbers here, so a
   metric the sheet does not carry simply does not appear.

A player with no config file still runs: it inherits the base profile and is
bound to its discovered sheet. That is what makes adding a player to the
workbook enough to make it available.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from .discovery import PlayerSheet, discover_players

CONFIG_DIR = Path(__file__).parent / "config"
PLAYERS_DIR = CONFIG_DIR / "players"

from .domain import VIRTUAL_ROW_BASE


def player_key(name: str) -> str:
    """'Marks & Spencer' -> 'marks_spencer'. Stable, filesystem-safe."""
    k = re.sub(r"[^a-z0-9]+", "_", name.strip().lower())
    return k.strip("_") or "player"


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_base() -> dict:
    return json.loads((CONFIG_DIR / "base_furniture.json").read_text(encoding="utf-8"))


def player_overrides(key: str) -> dict:
    p = PLAYERS_DIR / f"{key}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def has_player_config(key: str) -> bool:
    return (PLAYERS_DIR / f"{key}.json").exists()


def resolve_config(player: str, sheet: PlayerSheet | None = None,
                   reference_path: str | None = None) -> dict:
    """Merge base + player override, then bind metric par_ids to sheet rows."""
    key = player_key(player)
    cfg = _deep_merge(load_base(), player_overrides(key))
    cfg.setdefault("player", key)
    cfg.setdefault("display_name", player)

    if sheet is None and reference_path:
        try:
            matches = [p for p in discover_players(reference_path)
                       if player_key(p.name) == key or p.sheet == player]
            sheet = matches[0] if matches else None
        except Exception:
            # An unreadable workbook is the reader's problem to report, not a
            # reason to fail configuration.
            sheet = None

    if sheet is None:
        # Nothing to bind against, so only rows the player file pins itself apply.
        cfg["platform_rows"] = dict(cfg.get("metric_rows") or {})
        return cfg

    cfg["display_name"] = cfg.get("display_name") or sheet.name
    ref = cfg.setdefault("reference", {})
    ref["model_sheet"] = cfg.get("reference", {}).get("model_sheet") or sheet.sheet
    ref["header_row"] = sheet.header_row
    ref["first_month_col"] = sheet.first_month_col
    ref["first_data_row"] = sheet.header_row + 1
    ref.setdefault("last_data_row", max(sheet.par_ids.values(), default=sheet.header_row) + 40)

    # par_id -> row for this player. A metric the sheet does not carry still gets
    # a slot, placed well below the sheet, so a value that is *derivable* can be
    # computed and reported even though the player's own sheet has no row for it.
    # Such a slot has no history, so nothing can be estimated into it â€” it stays
    # null unless a formula fills it.
    rows: dict[str, int] = {}
    unresolved: list[str] = []
    virtual = VIRTUAL_ROW_BASE
    for name, par in (cfg.get("metrics") or {}).items():
        if name.startswith("_") or not isinstance(par, int):
            continue
        row = sheet.par_ids.get(par)
        if row:
            rows[name] = row
        else:
            rows[name] = virtual
            virtual += 1
            unresolved.append(f"{name} (par {par})")

    # A player file may pin rows the workbook gives no par_id for.
    rows.update({k: v for k, v in (cfg.get("metric_rows") or {}).items()
                 if isinstance(v, int)})

    cfg["platform_rows"] = rows
    cfg["unresolved_metrics"] = unresolved
    cfg["sheet_profile"] = {
        "sheet": sheet.sheet, "metrics_in_sheet": sheet.n_metrics,
        "months": len(sheet.months), "latest_month": sheet.latest_month,
        "resolved": len(rows), "unresolved": len(unresolved),
    }
    return cfg


def available_players(reference_path: str | None = None,
                      root: str | Path = "data") -> list[dict[str, Any]]:
    """The BPC players this system covers, with how far each one's data runs.

    Only players in config/furniture_players.json appear. "data to" counts months
    saved to history as well as those in the reference.
    """
    from .history import overlay_months

    out: dict[str, dict[str, Any]] = {}
    if reference_path:
        for s in discover_players(reference_path):
            k = player_key(s.name)
            saved = overlay_months(root, k)
            latest = max([m for m in [s.latest_month, *saved] if m], default=None)
            out[k] = {"key": k, "name": s.name, "sheet": s.sheet,
                      "configured": has_player_config(k), "metrics": s.n_metrics,
                      "latest_month": latest, "usable": s.usable,
                      "missing_core": ", ".join(s.missing_core), "saved_months": saved}
    return sorted(out.values(), key=lambda r: (not r["usable"], r["name"].lower()))

