"""Run persistence: inputs, configuration, results, overrides and history.

A run directory is self-describing — the uploaded files, the weight
configuration in force, the findings, the produced values and any manual
overrides all sit together, so a month can be reproduced or audited later.
"""

from __future__ import annotations

import datetime as _dt
import json
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RUNS = "runs"
CONFIGS = "saved_configs"


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


@dataclass
class Override:
    metric: str
    row: int | None
    original: float | None
    new_value: float
    reason: str
    author: str = ""
    at: str = field(default_factory=_now)
    par: int | None = None            # the metric's par_id, for figures with no sheet row
    unit: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric, "row": self.row, "original": self.original,
            "new_value": self.new_value, "reason": self.reason,
            "author": self.author, "at": self.at, "par": self.par,
        }


class Store:
    def __init__(self, root: str | Path = "data"):
        self.root = Path(root)
        (self.root / RUNS).mkdir(parents=True, exist_ok=True)
        (self.root / CONFIGS).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ runs

    def new_run(self, player: str, month: str) -> str:
        rid = f"{player.lower()}_{month}_{_dt.datetime.now():%Y%m%d-%H%M%S}_{uuid.uuid4().hex[:6]}"
        (self.root / RUNS / rid / "inputs").mkdir(parents=True, exist_ok=True)
        (self.root / RUNS / rid / "outputs").mkdir(parents=True, exist_ok=True)
        return rid

    def run_dir(self, run_id: str) -> Path:
        return self.root / RUNS / run_id

    def save_input(self, run_id: str, src: str | Path, kind: str) -> str:
        src = Path(src)
        dst = self.run_dir(run_id) / "inputs" / f"{kind}__{src.name}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return str(dst)

    def save_manifest(self, run_id: str, manifest: dict) -> None:
        manifest = dict(manifest)
        manifest.setdefault("run_id", run_id)
        manifest.setdefault("created_at", _now())
        self._write(self.run_dir(run_id) / "manifest.json", manifest)

    def save_result(self, run_id: str, result: dict) -> None:
        self._write(self.run_dir(run_id) / "result.json", result)

    def load_manifest(self, run_id: str) -> dict:
        return self._read(self.run_dir(run_id) / "manifest.json")

    def load_result(self, run_id: str) -> dict:
        return self._read(self.run_dir(run_id) / "result.json")

    def list_runs(self, player: str | None = None) -> list[dict]:
        out = []
        for d in sorted((self.root / RUNS).iterdir(), reverse=True):
            if not d.is_dir():
                continue
            try:
                m = self._read(d / "manifest.json")
            except FileNotFoundError:
                continue
            if player and m.get("player", "").lower() != player.lower():
                continue
            m["run_id"] = d.name
            out.append(m)
        return out

    # ------------------------------------------------------------- overrides

    def save_overrides(self, run_id: str, overrides: list[Override]) -> None:
        """Overrides are stored beside the run, never written back over inputs."""
        self._write(self.run_dir(run_id) / "overrides.json",
                    [o.to_dict() for o in overrides])

    def load_overrides(self, run_id: str) -> list[dict]:
        try:
            return self._read(self.run_dir(run_id) / "overrides.json")
        except FileNotFoundError:
            return []

    # ----------------------------------------------------- saved weight sets

    def save_weights(self, name: str, weights: dict, player: str | None = None) -> Path:
        key = f"{(player or 'global').lower()}__{name}.json"
        p = self.root / CONFIGS / key
        payload = dict(weights)
        payload["_saved_at"] = _now()
        payload["_player"] = player or "global"
        payload["_name"] = name
        self._write(p, payload)
        return p

    def list_weights(self, player: str | None = None) -> list[dict]:
        out = []
        for p in sorted((self.root / CONFIGS).glob("*.json")):
            try:
                d = self._read(p)
            except Exception:
                continue
            if player and d.get("_player") not in (player.lower(), "global"):
                continue
            d["_path"] = str(p)
            out.append(d)
        return out

    def load_weights(self, path: str | Path) -> dict:
        return self._read(Path(path))

    # ------------------------------------------------- cached player history

    def cache_history(self, player: str, payload: dict) -> None:
        self._write(self.root / f"history_{player.lower()}.json", payload)

    def load_history(self, player: str) -> dict | None:
        p = self.root / f"history_{player.lower()}.json"
        return self._read(p) if p.exists() else None

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _write(path: Path, obj: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")

    @staticmethod
    def _read(path: Path) -> Any:
        if not path.exists():
            raise FileNotFoundError(path)
        return json.loads(path.read_text(encoding="utf-8"))
