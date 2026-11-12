"""Run several players in one go.

Each player is run exactly as it would be on its own: its own files, its own history, its
own output folder. A failure in one player never stops the others, and a player with no
files simply runs on history. Files are matched to players by name, and the match is shown
for correction before anything runs.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .domain import month_label, shift_months
from .pipeline import RunResult, run_month
from .reference_store import load_player_list, workbook_has_player
from .store import Store


def match_player(filename: str) -> str | None:
    """The player a file name points to, or None. The longest matching alias wins."""
    low = filename.lower()
    best: tuple[int, str] | None = None
    for p in load_player_list()["players"]:
        for a in p.get("aliases", [p["name"].lower()]):
            if a in low and (best is None or len(a) > best[0]):
                best = (len(a), p["name"])
    return best[1] if best else None


def default_batch() -> list[str]:
    return list(load_player_list().get("batch_default", []))


@dataclass
class BatchItem:
    player: str
    status: str                       # done | stopped | error
    result: RunResult | None = None
    message: str = ""
    files: dict[str, str] = field(default_factory=dict)

    @property
    def folder(self) -> str:
        if self.result and self.result.outputs:
            return str(Path(next(iter(self.result.outputs.values()))).parent)
        return ""


@dataclass
class BatchResult:
    month: str
    items: list[BatchItem] = field(default_factory=list)

    def _growth(self, r, par: int, v, back: int) -> float | None:
        """% change of a figure on the stored history `back` months earlier (None if no basis)."""
        row = r.reference.par_to_row.get(par) if r.reference else None
        if v is None or v.is_null or not row:
            return None
        base = r.reference.value(row, shift_months(self.month, back))
        return (v.value / base - 1) * 100 if base else None

    def rows(self, gross_par: int = 241, shipped_par: int = 243) -> list[dict[str, Any]]:
        out = []
        for it in self.items:
            row: dict[str, Any] = {"player": it.player, "status": it.status,
                                   "files used": ", ".join(sorted(it.files)) or "history only",
                                   "message": it.message}
            r = it.result
            if r is not None and r.estimate is not None:
                g = r.estimate.value_for_par(r.reference, gross_par)
                s = r.estimate.value_for_par(r.reference, shipped_par)
                row.update({
                    "gross GMV": None if g is None or g.is_null else g.value,
                    "shipped GMV": None if s is None or s.is_null else s.value,
                    "MoM": self._growth(r, gross_par, g, -1), "YoY": self._growth(r, gross_par, g, -12),
                    "source": g.provenance.value if g is not None and not g.is_null else "",
                    "errors": len(r.errors), "warnings": len(r.warnings),
                    "confidence": r.weights.confidence.value if r.weights else ""})
            elif r is not None:
                row.update({"errors": len(r.errors), "warnings": len(r.warnings)})
            out.append(row)
        return out

    def output_files(self) -> list[tuple[str, Path]]:
        """(archive name, path) for every file produced."""
        files = []
        for it in self.items:
            if it.result is None:
                continue
            for p in it.result.outputs.values():
                pth = Path(p)
                if pth.exists():
                    files.append((f"{it.player}/{pth.name}", pth))
        return files

    collated: str | None = None          # the all-players comparison workbook

    def write_collated(self, out_dir: str | Path) -> str | None:
        """One workbook setting every finished player side by side. None if fewer than two."""
        from .export.collated import write_collated_workbook
        done = [i.result for i in self.items if i.status == "done" and i.result is not None]
        if len(done) < 2:
            return None
        p = Path(out_dir) / f"{zip_name(self.month)[:-4]} - Comparison.xlsx"
        p.parent.mkdir(parents=True, exist_ok=True)
        self.collated = str(write_collated_workbook(p, done, self.month))
        return self.collated

    def zip_to(self, path: str | Path) -> Path | None:
        files = self.output_files()
        if self.collated and Path(self.collated).exists():
            files = [("All players - " + Path(self.collated).name.split(" - ")[-1], Path(self.collated))] + files
        if not files:
            return None
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for arc, pth in files:
                z.write(pth, arc)
        return path

    @property
    def done(self) -> int:
        return sum(1 for i in self.items if i.status == "done")


def run_batch(
    players: list[str],
    month: str,
    files_by_player: dict[str, dict[str, str]] | None = None,
    reference_path: str | list[str] | None = None,
    weights_cfg: dict | None = None,
    out_dir: str | Path = "outputs",
    store: Store | None = None,
    seasonality_mode: str = "auto",
    history_weight: float | None = None,
    save_history: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
) -> BatchResult:
    """Run each player in turn. `files_by_player[player]` maps kind -> path (mis, transcript, ...)."""
    files_by_player = files_by_player or {}
    store = store or Store()
    res = BatchResult(month=month)
    for i, name in enumerate(players):
        if progress:
            progress(i, len(players), name)
        files = dict(files_by_player.get(name, {}))
        extra = {k: v for k, v in files.items()
                 if k in ("traffic", "logistics", "social", "brand_level", "secondary", "other")}
        refs = reference_path if isinstance(reference_path, (list, tuple)) else [reference_path]
        mine = next((p for p in refs if p and workbook_has_player(p, name)), None)
        try:
            r = run_month(player=name, month=month, reference_path=mine,
                          mis_path=files.get("mis"), transcript_path=files.get("transcript"),
                          weights_cfg=weights_cfg, out_dir=out_dir, store=store,
                          extra_sources=extra, seasonality_mode=seasonality_mode,
                          history_weight=history_weight, save_history=save_history,
                          prior_call_path=files.get("prior_call"),
                          prior_call_month=files.get("prior_call_month"))
        except Exception as e:                      # one player must never stop the rest
            res.items.append(BatchItem(name, "error", None, f"{type(e).__name__}: {e}", files))
            continue
        if r.estimate is None:
            why = "; ".join(f.message for f in r.errors[:2]) or "stopped by validation"
            res.items.append(BatchItem(name, "stopped", r, why, files))
        else:
            note = "" if r.ok else f"{len(r.errors)} error(s) to review"
            res.items.append(BatchItem(name, "done", r, note, files))
    try:
        res.write_collated(out_dir)
    except Exception:                     # the per-player files are the deliverable; this is extra
        res.collated = None
    if progress:
        progress(len(players), len(players), "")
    return res


def zip_name(month: str) -> str:
    return f"All players {month_label(month).replace(chr(39), ' 20')}.zip"
