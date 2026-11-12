"""End-to-end monthly run, independent of any user interface.

`run_month` is the whole product: give it a player, a month and the files that
exist, and it validates, weights, estimates, calibrates and exports. The
Streamlit app and the CLI are both thin callers of this function, so the
calculation engine can be tested on its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config_resolver import (available_players as _available_players,
                              has_player_config, player_key, resolve_config)
from .domain import Finding, Severity, month_label, shift_months
from .history import apply_overlay, calibration_records, save_month
from .reference_store import default_reference, resolve_reference, workbook_has_player
from .engine import calibrate as cal_mod
from .engine.corroborate import corroborate
from .engine.estimate import Estimator, MonthEstimate
from .engine.validate import blocking, validate_inputs
from .engine.weights import WeightResolution, resolve_weights
from .export import workbook as xl
from .readers.mis import MisData, load_mis
from .readers.reference import ReferenceModel, load_reference
from .readers.secondary import read_secondary
from . import learning as learn_mod
from .readers.transcript import TranscriptData, load_transcript
from .store import Override, Store

CONFIG_DIR = Path(__file__).parent / "config"


def load_player_config(player: str, reference_path: str | None = None) -> dict:
    """Resolve a player's working config: base profile + overrides + its sheet.

    A player with no config file of its own still resolves, inheriting the base
    fashion profile, so adding a sheet to the workbook is enough.
    """
    reference_path = reference_path or default_reference()
    cfg = resolve_config(player, reference_path=reference_path)
    if not cfg.get("platform_rows") and not reference_path:
        # Nothing to bind against and no player file: say so rather than
        # returning a config that would silently produce nothing.
        if not has_player_config(player_key(player)):
            have = sorted(x.stem for x in (CONFIG_DIR / "players").glob("*.json"))
            raise FileNotFoundError(
                f"No configuration for player '{player}' and no reference workbook to "
                f"discover it from. Configured players: {', '.join(have) or 'none'}.")
    return cfg


def load_default_weights() -> dict:
    return json.loads((CONFIG_DIR / "weights_default.json").read_text(encoding="utf-8"))


def available_players(reference_path: str | None = None) -> list[str]:
    """Player keys that can be run. Pass a workbook to include discovered ones."""
    return [p["key"] for p in list_players(reference_path)]


def list_players(reference_path: str | None = None, root: str | Path = "data") -> list[dict]:
    return _available_players(reference_path or default_reference(), root)


def commit_history(res: "RunResult", store: Store) -> dict:
    """Save a finished run's figures as that month's history."""
    if res.estimate is None or res.reference is None:
        raise ValueError("Nothing to save: the run produced no estimate.")
    key = player_key(load_player_config(res.player)["player"])
    anchored = bool(res.mis and res.month in res.mis.months)
    rc = res.estimate.reconciliations.get(241)
    cal = {}
    if rc is not None and rc.balanced_level is not None:
        cal = {"mis_level": rc.level, "output_level": rc.final_level,
               "history_level": rc.balanced_level, "weight": rc.blend_weight,
               "history_gap": abs(rc.balanced_level / rc.level - 1)}
    if res.learning:
        cal.update(res.learning)
    return save_month(store.root, key, res.month, res.history_estimate, res.reference,
                      res.run_id, anchored, cal)


def _pretty_month(month: str) -> str:
    """'2026-09' -> 'Sep 2026'. Safe in a filename on every platform."""
    import datetime as _dt
    y, m = month.split("-")
    return f"{_dt.date(int(y), int(m), 1):%b} {y}"


def _unique_dir(root: Path, name: str) -> Path:
    """`root/name`, or `root/name (2)` if a run for this month already exists."""
    root.mkdir(parents=True, exist_ok=True)
    p = root / name
    if not p.exists():
        return p
    n = 2
    while (root / f"{name} ({n})").exists():
        n += 1
    return root / f"{name} ({n})"


@dataclass
class RunResult:
    run_id: str
    player: str
    month: str
    ok: bool
    findings: list[Finding]
    estimate: MonthEstimate | None = None
    calibration: Any = None
    weights: WeightResolution | None = None
    reference: ReferenceModel | None = None
    transcript: TranscriptData | None = None
    mis: MisData | None = None
    outputs: dict[str, str] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)
    corroboration: Any = None
    mis_only: Any = None                  # the same run with the history weight at 0
    learning: dict | None = None          # this month's gap to the MIS and the expert
    config: dict | None = None            # resolved against the reference actually used

    @property
    def history_estimate(self):
        """What is saved as history: the MIS-based figures, not the balanced output."""
        return self.mis_only or self.estimate

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.all_findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.all_findings if f.severity is Severity.WARNING]

    @property
    def all_findings(self) -> list[Finding]:
        return self.findings + (self.estimate.findings if self.estimate else [])

    def summary(self) -> dict[str, Any]:
        P = self.estimate and self.estimate.rows
        return {
            "run_id": self.run_id, "player": self.player, "month": self.month,
            "ok": self.ok,
            "errors": len(self.errors), "warnings": len(self.warnings),
            "confidence": self.weights.confidence.value if self.weights else "none",
            "outputs": dict(self.outputs),
        }


# Metrics with enough history to be backtested honestly.
_BACKTEST = {
    "Cancellation %": "cancellation_pct",
    "Basket size": "basket_size",
    "MAU": "mau",
    "MAC": "mac",
    "COD share": "cod_pct",
}


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _months_between(a: str, b: str) -> int:
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])


def infer_call_month(path: str, data=None) -> str | None:
    """The month a call is about, from its file name or title ("Aug 2026", "Aug'26", "august-26")."""
    import re
    for text in (Path(path).stem, getattr(data, "title", "") or ""):
        m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s_\-']*"
                      r"(?:20)?(\d{2})\b", text.lower())
        if m:
            return f"20{m.group(2)}-{_MONTHS[m.group(1)]:02d}"
    return None


def run_month(
    player: str,
    month: str,
    reference_path: str | None = None,
    mis_path: str | None = None,
    transcript_path: str | None = None,
    weights_cfg: dict | None = None,
    out_dir: str | Path = "outputs",
    store: Store | None = None,
    overrides: list[Override] | None = None,
    extra_sources: dict[str, str] | None = None,
    mis_sheet: str | None = None,
    seasonality_mode: str = "auto",
    save_history: bool = False,
    history_weight: float | None = None,
    growth_override: dict | None = None,
    prior_call_path: str | None = None,
    prior_call_month: str | None = None,
) -> RunResult:
    """Run one reporting month end to end.

    With no `reference_path` the built-in reference is used, so only the
    month's own files (MIS, call, supporting data) ever need supplying.
    """
    builtin = default_reference()
    fallback_note = ""
    if reference_path and builtin and not workbook_has_player(reference_path, player):
        # An uploaded workbook that holds other players (or one player) cannot serve this one.
        fallback_note = (f"The uploaded reference has no sheet for {player}, so the built-in history "
                         "was used for it.")
        reference_path = None
    reference_path = resolve_reference(reference_path)
    is_builtin = bool(builtin) and Path(reference_path).resolve() == Path(builtin).resolve()
    cfg = load_player_config(player, reference_path)
    weights_cfg = weights_cfg or load_default_weights()
    store = store or Store()
    overrides = overrides or []
    extra_sources = extra_sources or {}

    run_id = store.new_run(player, month)
    findings: list[Finding] = []
    if fallback_note:
        findings.append(Finding("REFERENCE_FALLBACK", Severity.INFO, fallback_note))

    # ---- read inputs -------------------------------------------------------
    ref = mis = tr = None
    inputs: list[dict[str, str]] = []

    def record(kind: str, path: str | None, status: str, note: str = "", period: str = "") -> None:
        inputs.append({"kind": kind, "file": Path(path).name if path else "",
                       "period": period, "status": status, "note": note})

    if reference_path:
        try:
            ref = load_reference(reference_path, cfg)
            saved_notes = apply_overlay(ref, store.root, player_key(cfg["player"]),
                                        authoritative=not is_builtin)
            if not is_builtin:
                store.save_input(run_id, reference_path, "reference")
            record("built-in reference" if is_builtin else "reference workbook",
                   reference_path, "read",
                   f"{len(ref.months)} months, {len(ref.par_to_row)} par_ids"
                   + (f"; {len(saved_notes)} saved month(s) applied" if saved_notes else ""),
                   f"{month_label(ref.months[0])}–{month_label(ref.months[-1])}" if ref.months else "")
        except Exception as e:
            findings.append(Finding("REF_READ_FAILED", Severity.ERROR,
                                    "Could not read the reference workbook.",
                                    f"{type(e).__name__}: {e}", blocking=True))
            record("reference workbook", reference_path, "failed", str(e))
    else:
        record("reference workbook", None, "missing")

    if mis_path:
        try:
            mis = load_mis(mis_path, mis_sheet)
            store.save_input(run_id, mis_path, "mis")
            record("player MIS", mis_path, "read",
                   f"{len(mis.sections)} sections",
                   ", ".join(month_label(m) for m in mis.months))
        except Exception as e:
            findings.append(Finding("MIS_READ_FAILED", Severity.ERROR,
                                    "Could not read the player MIS.",
                                    f"{type(e).__name__}: {e}", blocking=True))
            record("player MIS", mis_path, "failed", str(e))
    else:
        record("player MIS", None, "missing")

    if transcript_path:
        try:
            tr = load_transcript(transcript_path)
            store.save_input(run_id, transcript_path, "transcript")
            record("expert call transcript", transcript_path, "read",
                   f"{len(tr.insights)} insights, {len(tr.category_yoy)} category YoY",
                   month_label(month))
        except Exception as e:
            findings.append(Finding("CALL_READ_FAILED", Severity.WARNING,
                                    "Could not read the expert call transcript.",
                                    f"{type(e).__name__}: {e}"))
            record("expert call transcript", transcript_path, "failed", str(e))
    else:
        record("expert call transcript", None, "missing")

    # The last known call stands in for this month's only when there is none for the month.
    prior_call, call_age = None, 0
    if prior_call_path and tr is None:
        try:
            prior_call = load_transcript(prior_call_path)
            pm = prior_call_month or infer_call_month(prior_call_path, prior_call) \
                or shift_months(month, -1)
            call_age = max(1, _months_between(pm, month))
            store.save_input(run_id, prior_call_path, "prior_call")
            record("last known expert call", prior_call_path, "read",
                   f"{len(prior_call.category_yoy)} category YoY, {call_age} month(s) old "
                   "- used for category direction only", month_label(pm))
            findings.append(Finding(
                "PRIOR_CALL_USED", Severity.INFO,
                f"No call for {month_label(month)}, so the last known call "
                f"({month_label(pm)}, {call_age} month(s) old) informs the category mix.",
                "Its category growth, relative to the platform at that time, tilts the history-based "
                "category mix by a damped amount. It does not set any level."))
            prior_call.month = pm
        except Exception as e:
            prior_call = None
            findings.append(Finding("PRIOR_CALL_FAILED", Severity.WARNING,
                                    "Could not read the last known call.",
                                    f"{type(e).__name__}: {e}"))
            record("last known expert call", prior_call_path, "failed", str(e))
    elif prior_call_path:
        record("last known expert call", prior_call_path, "unused",
               "A call for this month was supplied, so the older call is not needed.")

    secondary = None
    for kind, p in extra_sources.items():
        store.save_input(run_id, p, kind)
        if kind == "secondary":
            try:
                from .reference_store import load_player_list
                plist = load_player_list()["players"]
                own = next((x.get("aliases", []) for x in plist
                            if x["name"].lower() == cfg["player"].lower()), [])
                own = list({a for a in own} | {cfg["player"].lower()})
                others = [a for x in plist if x["name"].lower() != cfg["player"].lower()
                          for a in x.get("aliases", []) if len(a) >= 3]
                secondary = read_secondary(p, own, others)
                record(kind, p, "read", f"{len(secondary.items)} items for {cfg['player']}, "
                       f"{secondary.skipped} about other players")
            except Exception as e:
                findings.append(Finding("SECONDARY_READ_FAILED", Severity.WARNING,
                                        "Could not read the secondary file.",
                                        f"{type(e).__name__}: {e}"))
                record(kind, p, "failed", str(e))
            continue
        record(kind, p, "stored",
               "No reader is configured for this source type; it is retained with the run "
               "but contributes no numbers.")

    # ---- validate ----------------------------------------------------------
    findings += validate_inputs(cfg, month, ref, mis, tr)
    hard = blocking(findings)
    if hard or ref is None:
        store.save_manifest(run_id, {
            "player": player, "month": month, "inputs": inputs,
            "status": "blocked",
            "blocking": [f.to_dict() for f in hard],
        })
        return RunResult(run_id, player, month, ok=False, findings=findings,
                         manifest={"inputs": inputs, "run_id": run_id})

    # ---- weights -----------------------------------------------------------
    availability = {
        "expert_call": tr is not None or prior_call is not None,
        "player_mis": mis is not None and month in mis.months,
        "logistics": "logistics" in extra_sources,
        "traffic": "traffic" in extra_sources,
        "seasonality": True,       # derived from the calendar; always available
        "market_events": True,     # derived from known platform sale calendars
        "social": "social" in extra_sources,
        "brand_level": "brand_level" in extra_sources,
        "secondary": secondary is not None and bool(secondary.usable),
    }
    quality: dict[str, list[tuple[str, float]]] = {}
    if tr is not None and tr.transcription_quality in ("mixed", "low", "poor"):
        rule = weights_cfg.get("quality_rules", {}).get("low_transcription_confidence", {})
        quality["expert_call"] = [("transcript contains garbled or inconsistent passages",
                                   rule.get("multiplier", 0.6))]
    if prior_call is not None:
        rules = weights_cfg.get("quality_rules", {})
        stale = rules.get("stale_months", {"after_months": 2, "multiplier": 0.5})
        m_ = stale.get("multiplier", 0.5) if call_age > stale.get("after_months", 2) else 0.7
        quality["expert_call"] = [(f"call is from {call_age} month(s) earlier", m_)]
    wres = resolve_weights(weights_cfg, availability, quality)
    for w in wres.warnings:
        findings.append(Finding("WEIGHTS", Severity.WARNING, w))

    # ---- estimate ----------------------------------------------------------
    key = player_key(cfg["player"])
    past_cal = calibration_records(store.root, key, month)
    estimator = Estimator(cfg, ref, month, mis, tr, wres.effective,
                          seasonality_mode=seasonality_mode, history_weight=history_weight,
                          past_calibration=past_cal, growth_override=growth_override,
                          secondary=secondary, prior_call=prior_call)
    est = estimator.run()

    # The same run with the history weight at 0 is the figures as the MIS reports them. It is
    # what gets saved as history (so the gap to the MIS narrows month by month) and what the
    # output is compared against.
    mis_only = None
    rc = est.reconciliations.get(241)
    if rc is not None and rc.final_level is not None and abs(rc.final_level / rc.level - 1) > 1e-9:
        mis_only = Estimator(cfg, ref, month, mis, tr, wres.effective,
                             seasonality_mode=seasonality_mode, history_weight=0.0,
                             past_calibration=past_cal, growth_override=growth_override).run()

    # ---- learning record: how far the model's own view was from the MIS and the expert ----
    learn, expert_rows = None, []
    try:
        pred = Estimator(cfg, ref, month, None, None, wres.effective,
                         seasonality_mode=seasonality_mode, past_calibration=past_cal).run() \
            if (mis is not None and month in mis.months) else est
        g = est.value_for_par(ref, 241)
        stated = tr.stated_values.get("Platform GMV") if tr is not None else None
        pgap = (stated / g.value - 1) if stated and g is not None and g.value else None
        eg = estimator.expert_gaps()
        expert_rows = list({v["label"]: v for v in eg.values()}.values())
        learn = learn_mod.measure(est, pred, eg, estimator.learned, pgap)
    except Exception as e:                       # learning must never stop a run
        findings.append(Finding("LEARNING_SKIPPED", Severity.INFO,
                                "The learning record could not be built for this run.",
                                f"{type(e).__name__}: {e}"))

    # ---- apply overrides ---------------------------------------------------
    for o in overrides:
        par = getattr(o, "par", None)
        row = o.row if o.row in est.rows else (ref.par_to_row.get(par) if par else None)
        cur = est.rows.get(row) if row is not None else None
        if cur is None and par is not None:
            cur = est.by_par.get(par)
        if cur is None and not (par is not None and getattr(o, "unit", "")):
            continue
        if cur is not None and cur.provenance.is_actual:
            est.findings.append(Finding(
                "OVERRIDE_ON_ACTUAL", Severity.WARNING,
                f"Override applied to '{o.metric}', which is a reported actual.",
                "The original reported value is retained in the override log and in the "
                "source data; only the exported figure changed.", metric=o.metric))
        old = cur.value if cur is not None and not cur.is_null else None
        o.original = old
        from .domain import Confidence, Provenance, Value
        new = Value(
            o.new_value, Provenance.ASSUMED, cur.unit if cur is not None else o.unit,
            source=f"manual override by {o.author or 'user'}",
            method=f"manual override: {o.reason}", confidence=Confidence.LOW,
            uncalibrated=old,
        ).with_note(f"original {old} ({cur.provenance.value if cur is not None else 'none'}) "
                    "retained in the override log")
        for r_, v_ in list(est.rows.items()):          # every place that holds the old figure
            if cur is not None and v_ is cur:
                est.rows[r_] = new
        if row is not None and row in est.rows or (row is not None and row >= 0 and cur is None):
            est.rows[row] = new
        if par is not None:
            est.by_par[par] = new
    if overrides:
        store.save_overrides(run_id, overrides)

    # ---- calibrate ---------------------------------------------------------
    secs = cfg.get("mis_sections", {})
    mis_cat = ({k: v / estimator.mis_scale for k, v in mis.section_values(
                    month, secs.get("category_gmv", "Category GMV")).items()}
               if mis and month in mis.months else None)
    backtest_rows = {label: cfg["platform_rows"][key] for label, key in _BACKTEST.items()
                     if key in cfg["platform_rows"]}
    cal = cal_mod.calibrate(cfg, ref, month, mis_cat, tr, backtest_rows)

    # Independent cross-checks of the MIS, using expert figures that owe nothing
    # to it. These test the reported level rather than produce it.
    def _plat(k):
        v = est.get(cfg["platform_rows"].get(k, -1))
        return None if v is None else (v.uncalibrated if v.uncalibrated else v.value)

    plat = {k: _plat(k) for k in ("gross_gmv", "shipped_gmv", "net_gmv")}   # as the MIS reports
    cor, cor_findings = corroborate(cfg, mis_cat, tr, plat)
    findings += cor_findings

    # ---- export ------------------------------------------------------------
    # Readable folder and file names: "Myntra Sep 2026". A repeat run for the
    # same month gets a " (2)" suffix rather than a timestamp.
    base = f"{cfg['display_name']} {_pretty_month(month)}"
    out_dir = _unique_dir(Path(out_dir), base)
    ctx = {
        "config": cfg, "reference": ref, "estimate": est, "calibration": cal,
        "corroboration": cor, "mis_only": mis_only, "past_calibration": past_cal,
        "expert_rows": expert_rows, "transcript": tr, "has_mis": bool(mis and month in mis.months),
        "weights": wres, "month": month, "transcript": tr, "findings": findings,
        "overrides": [o.to_dict() for o in overrides],
        "manifest": {"run_id": run_id, "inputs": inputs,
                     "config_version": weights_cfg.get("version", 1)},
    }
    outputs = {
        "deliverable": str(xl.write_output_workbook(out_dir / f"{base} - Output.xlsx", ctx)),
        "audit": str(xl.write_audit_workbook(out_dir / f"{base} - Audit.xlsx", ctx)),
        "model_column": str(xl.write_model_column(out_dir / f"{base} - Model Column.xlsx", ctx)),
        "analysis": str(xl.write_analysis_workbook(out_dir / f"{base} - Analysis.xlsx", ctx)),
    }

    manifest = {
        "player": player, "month": month, "inputs": inputs, "status": "complete",
        "config_version": weights_cfg.get("version", 1),
        "weights": wres.to_dict(),
        "outputs": outputs,
        "errors": sum(1 for f in findings + est.findings if f.severity is Severity.ERROR),
        "warnings": sum(1 for f in findings + est.findings if f.severity is Severity.WARNING),
    }
    store.save_manifest(run_id, manifest)
    store.save_result(run_id, {
        "summary": {k: est.num(cfg["platform_rows"].get(k, -1))
                    for k in ("shipped_gmv", "gross_gmv", "net_gmv", "aov", "asp")},
        "values": {str(k): v.to_dict() for k, v in est.rows.items()},
        "findings": [f.to_dict() for f in findings + est.findings],
        "adjustments": [a.to_dict() for a in est.adjustments],
        "calibration": cal.to_dict(),
        "corroboration": cor.to_dict(),
        "method_changes": est.method_changes,
        "notes": est.notes,
    })

    result = RunResult(
        run_id=run_id, player=player, month=month,
        ok=not any(f.severity is Severity.ERROR for f in findings + est.findings),
        findings=findings, estimate=est, calibration=cal, weights=wres,
        reference=ref, transcript=tr, mis=mis, outputs=outputs, manifest=manifest,
        corroboration=cor, config=cfg, mis_only=mis_only, learning=learn,
    )
    if save_history and result.ok:
        result.manifest["history_saved"] = commit_history(result, store)
    return result
