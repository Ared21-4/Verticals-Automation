"""Builds the reporting month's full metric column.

Order of precedence, fixed by the decisions recorded in METHODOLOGY.md:

1. MIS reported actuals anchor the level and the category mix. They are never
   moved by evidence weights.
2. Everything the reference model defines as a formula is recomputed from those
   anchors, so derived metrics cannot drift away from their inputs.
3. Only parameters with no direct evidence are estimated, by the seasonal-naive
   estimator in `estimators.py`, using history that ends before this month.
4. The expert call corroborates, contradicts and explains. Because it gave no
   platform figure this month, it moves no platform number; it is the sole
   basis for the historical category calibration in `calibrate.py`.

Each number carries its provenance, so the exporter can keep reported actuals,
derived values, estimates and assumptions visibly distinct.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain import (ABSENT_ROW, Adjustment, Confidence, Finding, MetricRows,
                      Provenance, Severity, Value, is_sheet_row, month_label, null,
                      shift_months)
from ..readers.mis import MisData
from ..readers.reference import ReferenceModel
from ..readers.transcript import TranscriptData
from .. import learning
from . import estimators as est
from .growth import (balanced_level, blend, reconcile_growth, trusted_weight)
from .mis_adapter import GenericMis, headline_check, read_generic_mis


@dataclass
class MonthEstimate:
    player: str
    month: str
    rows: dict[int, Value] = field(default_factory=dict)
    l2: dict[str, dict[str, Value]] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)
    drivers_claimed: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    method_changes: list[dict[str, str]] = field(default_factory=list)
    # Values for parameters this player's sheet has no row for (e.g. category
    # shipped GMV for players that store shares). Keyed by par_id.
    by_par: dict[int, Value] = field(default_factory=dict)
    # growth reconciliations, keyed by par_id (241 gross GMV, 240 AOV)
    reconciliations: dict[int, Any] = field(default_factory=dict)

    def set_par(self, par_id: int, v: Value) -> Value:
        self.by_par[par_id] = v
        return v

    def value_for_par(self, ref, par_id: int) -> Value | None:
        """A parameter's value, from its sheet row if it has one, else the derived store."""
        row = ref.par_to_row.get(par_id)
        v = self.get(row) if row else None
        return v if v is not None else self.by_par.get(par_id)

    def set(self, row: int, v: Value) -> Value:
        # A metric this player's sheet does not carry is simply not written.
        if row is None or row < 0:
            return v
        self.rows[row] = v
        return v

    def get(self, row: int) -> Value | None:
        if row is None or row < 0:
            return None
        return self.rows.get(row)

    def num(self, row: int) -> float | None:
        v = self.get(row)
        return None if v is None else v.value

    def finding(self, *a, **kw) -> None:
        self.findings.append(Finding(*a, **kw))

    def claim_driver(self, driver: str, by: str) -> bool:
        """Register a business driver. Returns False if already claimed elsewhere.

        This is the double-counting guard: if the expert call already explains an
        uplift, the seasonality lever must not apply it a second time.
        """
        if driver in self.drivers_claimed:
            return False
        self.drivers_claimed[driver] = by
        return True


class Estimator:
    def __init__(
        self,
        cfg: dict,
        ref: ReferenceModel,
        month: str,
        mis: MisData | None = None,
        transcript: TranscriptData | None = None,
        weights: dict[str, float] | None = None,
        seasonality_mode: str = "auto",
        history_weight: float | None = None,
        past_calibration: list | None = None,
        growth_override: dict | None = None,
        secondary=None,
        prior_call: TranscriptData | None = None,
    ):
        self.secondary = secondary
        self.prior_call = prior_call
        # What earlier months taught the category mix (see fashion_intel/learning.py).
        self.learned = learning.corrections(past_calibration or [])
        self.cfg = cfg
        self.ref = ref
        self.month = month
        self.mis = mis
        self.tr = transcript
        self.weights = weights or {}
        self.prior = shift_months(month, -1)
        self.yoy_month = shift_months(month, -12)
        # Missing metrics resolve to ABSENT_ROW rather than raising, so a player
        # whose sheet carries 50 metrics runs the same code path as one with 208.
        self.P = MetricRows(cfg.get("platform_rows") or {})
        self.S = cfg.get("segments") or {}
        self.L2 = cfg.get("l2_categories") or {}
        self.has_categories = bool(self.L2) and bool(self.S)
        self.out = MonthEstimate(player=cfg["player"], month=month)
        self._origins: dict[str, str] = {}
        self._l2_estimated_from_history = False
        self.gm: GenericMis | None = None
        self.mis_scale = 1.0
        self._call_stated_gmv: float | None = None
        self._locked_null_rows: set[int] = set()     # left null on purpose; never back-filled
        self.seasonality_mode = seasonality_mode
        self.history_weight = history_weight
        self.growth_override = growth_override or {}      # {'mom': x, 'yoy': y} replaces the MIS's
        self.past_calibration = past_calibration or []
        self.apply_seasonal, self.seasonal_reason = self._resolve_seasonality()

    def _resolve_seasonality(self) -> tuple[bool, str]:
        """Decide whether this month's historical seasonal factor is representative.

        Seasonal factors are learned from the same calendar month in prior years.
        If the recurring event that produced that seasonality did not fall in the
        month this year, the factor describes an event that did not happen and
        applying it would invent demand.
        """
        if self.seasonality_mode == "bau":
            return False, "seasonality suppressed by explicit run setting"
        if self.seasonality_mode == "festive":
            return True, ""
        shift = self.tr.event_shift if self.tr else None
        if shift and shift.detected:
            return False, (
                "the expert call establishes that the recurring sale event moved out of this "
                "month, so prior-year seasonality for this calendar month is not representative")
        return True, ""

    # ------------------------------------------------------------------ utils

    def _series(self, row: int) -> dict[str, float]:
        """Historical series for a row, strictly before the reporting month."""
        return {m: v for m, v in self.ref.series.get(row, {}).items() if m < self.month}

    def _prior(self, row: int) -> float | None:
        return self.ref.value(row, self.prior)

    def _estimate_row(
        self, row: int, label: str, unit: str = "", clip_tolerance: float = 0.15,
        strength: float = 1.0,
    ) -> Value:
        s = self._series(row)
        # A series whose last observation is long past says nothing about this
        # month. Projecting it forward would be inventing a number, so it stays null.
        max_age = self.cfg.get("max_history_age_months", 6)
        if s and max_age:
            last = max(s)
            ly, lm = int(last[:4]), int(last[5:7])
            ty, tm = int(self.month[:4]), int(self.month[5:7])
            age = (ty - ly) * 12 + (tm - lm)
            if age > max_age:
                return null(unit, f"{label}: history ends {month_label(last)}, {age} months "
                                  f"before {month_label(self.month)}; too stale to project")
        e = est.seasonal_naive(
            s, self.month, clip_tolerance=clip_tolerance,
            apply_seasonal=self.apply_seasonal,
            seasonal_suppression_reason=self.seasonal_reason,
            seasonal_strength=strength,
        )
        if not e.ok:
            return null(unit, f"no history for {label} before {month_label(self.month)}")
        v = Value(
            value=e.value, provenance=Provenance.ESTIMATED, unit=unit,
            source=f"{self.ref.model_sheet} row {row} history",
            method=f"{label}: {e.method}", confidence=e.confidence,
        )
        if e.clipped:
            v.with_note("clipped to the trailing 12-month range")
        return v

    def _estimate_gmv_history(self, row: int, label: str) -> Value:
        """A headline GMV with no MIS and no stated figure, built from history alone.

        This is the least reliable number the system produces, so it is made as safe as
        the data allows and labelled for what it is: the seasonal swing is damped by the
        amount the player's own past errors favour, and the result carries a range and a
        warning stating how wrong this method has been before.
        """
        s = self._series(row)
        strength, table, basis = (est.choose_strength(s, self.month) if self.apply_seasonal
                                  else (1.0, {}, "seasonality suppressed"))
        v = self._estimate_row(row, label, "INR Cr", clip_tolerance=0.35, strength=strength)
        if v.is_null:
            return v
        flat = est.seasonal_naive(s, self.month, clip_tolerance=0.35, apply_seasonal=False)
        full = est.seasonal_naive(s, self.month, clip_tolerance=0.35, seasonal_strength=1.0,
                                  apply_seasonal=self.apply_seasonal)
        lo, hi = sorted([x for x in (flat.value, full.value, v.value) if x is not None])[::2]
        v.confidence = Confidence.LOW
        v.with_note(f"History only: {lo:,.1f} (no seasonal uplift) to {hi:,.1f} (full uplift). "
                    f"Uplift strength {strength:.2f}: {basis}.")
        worst = table.get(strength, (None, None))[1]
        typical = table.get(strength, (None, None))[0]
        self.out.finding(
            "GMV_HISTORY_ONLY", Severity.WARNING,
            f"{label} of {v.value:,.1f} Cr is built from history alone (range {lo:,.1f} to "
            f"{hi:,.1f}).",
            ("No MIS and no GMV figure from a call was supplied. "
             + (f"On this player's own past months the method has averaged {typical:.0%} error "
                f"and missed by up to {worst:.0%}. " if typical is not None else "")
             + "It cannot know whether a sale or festival fell in this month, so a month that "
               "breaks the usual pattern can miss by much more. Supply the MIS, or a call "
               "that states the GMV, to replace it."),
            metric=label)
        return v

    def _carry(self, row: int, label: str, unit: str = "") -> Value:
        p = self._prior(row)
        if p is None:
            return null(unit, f"{label} has no prior-month value to carry forward")
        return Value(
            value=p, provenance=Provenance.ASSUMED, unit=unit,
            source=f"{self.ref.model_sheet} row {row} {month_label(self.prior)}",
            method=f"{label}: carried forward unchanged, no evidence this month",
            confidence=Confidence.LOW,
        )

    def _plausibility(self, key: str, row: int, value: float | None, label: str) -> None:
        bounds = self.cfg.get("plausibility_bounds", {}).get(key)
        if not bounds or value is None:
            return
        p = self._prior(row)
        if p is None:
            return
        if "max_abs_mom_delta" in bounds and abs(value - p) > bounds["max_abs_mom_delta"]:
            self.out.finding(
                "PLAUSIBILITY_MOM", Severity.WARNING,
                f"{label} moves {value - p:+.1%} month-on-month "
                f"({p:.1%} → {value:.1%}), beyond the configured "
                f"{bounds['max_abs_mom_delta']:.0%} limit.",
                "Applied as supplied because the MIS is the anchoring source, but the size of "
                "the move points to a definition change rather than a business movement.",
                metric=label)
        if "max_abs_mom_pct" in bounds and p and abs(value / p - 1) > bounds["max_abs_mom_pct"]:
            self.out.finding(
                "PLAUSIBILITY_MOM_PCT", Severity.WARNING,
                f"{label} moves {value / p - 1:+.1%} month-on-month.", metric=label)

    # ------------------------------------------------------------------- run

    def run(self) -> MonthEstimate:
        self._step_seasonality_context()
        self._step_generic_mis()
        self._step_mis_categories()
        self._step_platform_level()
        self._step_secondary()
        self._step_returns_and_derived()
        self._step_units_and_aov()
        self._step_users()
        self._step_segments()
        self._step_splits()
        self._step_shares()
        self._step_traffic()
        self._step_tail()
        self._step_generic_tree()
        self._step_template_fill()
        self._step_category_gmv_from_shares()
        self._step_price_buckets()
        self._step_sheet_rollups()
        self._step_own_model()
        self._step_item_blocks()
        self._step_own_model()
        self._step_identities()
        self._step_call_corroboration()
        self._step_history_base_check()
        self._step_growth_reconciliation()
        self._publish_virtual()
        return self.out

    def _publish_virtual(self) -> None:
        """Make values held in off-sheet slots addressable by par_id.

        A player whose sheet has no row for, say, Shipped GMV still gets one
        derived; this exposes it to the exporters under its par_id.
        """
        for name, par in (self.cfg.get("metrics") or {}).items():
            if not isinstance(par, int) or par in self.out.by_par:
                continue
            row = self.P[name]
            if row != ABSENT_ROW and not is_sheet_row(row):
                v = self.out.rows.get(row)
                if v is not None:
                    self.out.by_par[par] = v

    # --------------------------------------- 0a. the standard-template MIS

    def _step_generic_mis(self) -> None:
        """Read a MIS on the standard fashion template, for players without a tuned reader."""
        if not self.cfg.get("mis_generic") or not self.mis or self.month not in self.mis.months:
            return
        if self.has_categories:
            # Myntra's own reader: same unit detection, so a MIS in rupees is not read as crore.
            _, _, self.mis_scale, basis = headline_check(self.mis, self.month, self.cfg, self.ref)
            if self.mis_scale != 1.0:
                self.out.finding(
                    "MIS_UNITS", Severity.INFO,
                    f"MIS GMV figures divided by {self.mis_scale:,.0f} to give INR Cr.",
                    f"Detected from history: {basis}.", metric="gmv_scale")
            return
        ref_gross = self._prior(self.P["gross_gmv"]) if self.P.has("gross_gmv") else None
        self.gm = read_generic_mis(self.mis, self.month, self.cfg, ref_gross)
        self.out.findings.extend(self.gm.findings)

    def _put_par(self, par: int, v: Value) -> None:
        """Store a value under its par_id: on the sheet row if the player has one."""
        row = self.ref.par_to_row.get(par)
        if row:
            self.out.set(row, v)
        self.out.set_par(par, v)

    def _cancellation_value(self) -> Value:
        """The pre-shipment cancellation rate, from the MIS where it can be trusted.

        A MIS that lists both a cancellation rate and an RTO rate is ambiguous when
        the expert says the cancellation figure already contains RTO: taking both
        would count RTO twice. The call's own definition settles it.
        """
        P = self.P
        hist = self._estimate_row(P["cancellation_pct"], "Cancellation %", "%",
                                  clip_tolerance=0.10)
        c = self.gm.overall.get("cancellation_pct") if self.gm else None
        if c is None:
            return hist
        rto = self.gm.overall.get("rto_pct")
        src = f"MIS {month_label(self.month)}"
        if rto is not None and self.tr is not None and self.tr.cancellation_includes_rto \
                and c > rto:
            model = c - rto
            self.out.adjustments.append(Adjustment(
                metric="Cancellation %", driver="cancellation definition",
                before=c, after=model,
                rationale=(f"The MIS gives cancellations of {c:.0%} and RTO of {rto:.0%}. The "
                           "expert says the cancellation figure already includes RTO "
                           f"(\"{self.tr.cancellation_definition[:140]}\"). Taking both would "
                           f"count RTO twice, so pre-shipment cancellation is {c:.0%} less "
                           f"{rto:.0%} = {model:.0%} and RTO is carried separately."),
                source="expert call"))
            self.out.finding(
                "CANCELLATION_DEFINITION", Severity.WARNING,
                f"MIS cancellation of {c:.0%} read as including RTO ({rto:.0%}), so "
                f"pre-shipment cancellation is {model:.0%}.",
                f"Per the call. If the MIS figure is pre-shipment only, shipped GMV would be "
                f"{(1 - c):.0%} of gross rather than {(1 - model):.0%}. Confirm with the player.",
                metric="cancellation_pct")
            return Value(model, Provenance.DERIVED, "%", source=src,
                         method=f"MIS cancellations {c:.0%} less RTO {rto:.0%}, because the "
                                "call says cancellations include RTO",
                         confidence=Confidence.MEDIUM)
        if hist.value is not None and c > hist.value + 0.15:
            self.out.finding(
                "CANCELLATION_DEFINITION", Severity.WARNING,
                f"MIS cancellation of {c:.0%} is far above the player's history "
                f"({hist.value:.0%}).",
                "It may include RTO or other leakage, which the model counts separately. "
                "Used as supplied; supply the expert call or confirm the definition.",
                metric="cancellation_pct")
        return Value(c, Provenance.REPORTED, "%", source=src,
                     method="MIS reported cancellation rate", confidence=Confidence.HIGH)

    # ------------------------------------------------ 0. calendar / seasonality

    def _step_seasonality_context(self) -> None:
        """Settle the calendar lever once, and claim its driver.

        Claiming the driver here is what stops the same festive-timing effect
        being applied twice — once through the call's category YoY and again
        through a calendar uplift.
        """
        shift = self.tr.event_shift if self.tr else None
        if not self.apply_seasonal:
            self.out.claim_driver(
                "festive event timing shift",
                "calendar lever — prior-year seasonality for this month suppressed because the "
                "recurring sale event did not fall in it this year")
            ev = (shift.evidence[:4] if shift and shift.detected else [])
            self.out.adjustments.append(Adjustment(
                metric="all seasonally estimated parameters",
                driver="festive event timing shift",
                before=None, after=None,
                rationale=(
                    f"Seasonal factors for {month_label(self.month)} are learned from prior "
                    f"{month_label(self.month)[:3]} months, which were festive. "
                    f"{self.seasonal_reason.capitalize()}. Estimates therefore hold at the "
                    "trailing level rather than applying a festive uplift. "
                    + (" Evidence: " + " | ".join(ev) if ev else "")),
                source="expert call transcript" if shift and shift.detected
                       else "run setting",
            ))
            self.out.finding(
                "SEASONALITY_SUPPRESSED", Severity.INFO,
                f"Calendar seasonality suppressed for {month_label(self.month)}.",
                self.seasonal_reason.capitalize()
                + ". Applying the prior-year factor would have imported an event that did not "
                  "occur in this month.")
        else:
            self.out.claim_driver(
                "calendar seasonality",
                "calendar lever — prior-year seasonal factor applied normally")

    # ---------------------------------------------------- 1. MIS -> L2 mapping

    def _step_mis_categories(self) -> None:
        """Map MIS categories onto the model's L2 taxonomy, preserving totals."""
        secs = self.cfg.get("mis_sections", {})
        if not self.has_categories:
            # This player has no category taxonomy configured, so the run covers
            # the platform level only. That is reported, not silently assumed.
            self.out.finding(
                "NO_CATEGORY_TAXONOMY", Severity.INFO,
                f"No category taxonomy is configured for {self.cfg['display_name']}.",
                "The run produces platform-level metrics only. Category output needs a "
                "mis_taxonomy_map and l2_categories block in the player configuration.")
            return
        if self.mis is None or self.month not in self.mis.months:
            self.out.finding("NO_MIS_CATEGORIES", Severity.WARNING,
                             "No MIS category data; the category mix falls back to history.")
            self._l2_from_history()
            return

        gmv = {k: v / self.mis_scale for k, v in self.mis.section_values(
            self.month, secs.get("category_gmv", "Category GMV")).items()}
        asp = self.mis.section_values(self.month, secs.get("category_asp", "Category ASP"))
        if not gmv:
            self._l2_from_history()
            return

        # Units per MIS category: GMV in Cr, ASP in INR -> units in Mn.
        mis_units: dict[str, float] = {}
        for k, g in gmv.items():
            a = asp.get(k)
            if a and a > 0:
                mis_units[k] = g * 10.0 / a
            else:
                self.out.finding("MIS_ASP_MISSING", Severity.WARNING,
                                 f"MIS category '{k}' has GMV but no usable ASP.",
                                 "Units for this category are allocated from the historical "
                                 "unit mix instead of being derived.", metric=k)

        tmap = self.cfg.get("mis_taxonomy_map") or {}
        acc_gmv: dict[str, float] = {}
        acc_units: dict[str, float] = {}
        unmapped: list[str] = []
        self._origins: dict[str, str] = {}

        for mis_cat, g in gmv.items():
            spec = tmap.get(mis_cat)
            if spec is None:
                unmapped.append(mis_cat)
                continue
            targets = spec["targets"]
            u = mis_units.get(mis_cat)
            if len(targets) == 1:
                t = targets[0]
                # A second MIS line landing on the same target makes it a merge.
                self._origins[t] = "merge" if t in acc_gmv else "direct"
                acc_gmv[t] = acc_gmv.get(t, 0.0) + g
                if u is not None:
                    acc_units[t] = acc_units.get(t, 0.0) + u
            else:
                hist_g = {t: self.ref.value(self.L2[t]["gmv"], self.prior) or 0.0 for t in targets}
                hist_u = {t: self.ref.value(self.L2[t]["units"], self.prior) or 0.0 for t in targets}
                if sum(hist_g.values()) <= 0:
                    self.out.finding("SPLIT_NO_BASIS", Severity.ERROR,
                                     f"Cannot split MIS '{mis_cat}' across {targets}: "
                                     "no historical basis in the prior month.", metric=mis_cat)
                    continue
                for t, part in est.ratio_split(g, hist_g).items():
                    acc_gmv[t] = acc_gmv.get(t, 0.0) + part
                    self._origins[t] = "split"
                if u is not None and sum(hist_u.values()) > 0:
                    for t, part in est.ratio_split(u, hist_u).items():
                        acc_units[t] = acc_units.get(t, 0.0) + part
                self.out.notes.append(
                    f"MIS '{mis_cat}' split across {', '.join(targets)} using the "
                    f"{month_label(self.prior)} mix.")

        if unmapped:
            self.out.finding("MIS_UNMAPPED_CATEGORY", Severity.ERROR,
                             f"{len(unmapped)} MIS categories have no mapping to the model "
                             f"taxonomy: {', '.join(unmapped)}.",
                             "Their GMV is excluded from the category build; add them to "
                             "mis_taxonomy_map in the player configuration.")

        residual = self._carve_residual(acc_gmv, acc_units)

        # Provenance depends on how the figure reached the model taxonomy. Only a
        # category that maps one-to-one onto a MIS line is a reported actual; an
        # allocation out of a reported total is derived, and a category with no
        # MIS line at all is an estimate.
        meta_prov = {
            "direct": (Provenance.REPORTED, Confidence.HIGH,
                       "MIS category GMV mapped one-to-one onto the model taxonomy"),
            "merge": (Provenance.REPORTED, Confidence.HIGH,
                      "sum of the MIS categories that map onto this model category"),
            "split": (Provenance.DERIVED, Confidence.MEDIUM,
                      f"allocated out of a reported MIS total using the "
                      f"{month_label(self.prior)} mix"),
            "residual": (Provenance.ESTIMATED, Confidence.LOW,
                         f"no MIS line; estimated at the {month_label(self.prior)} share "
                         "of the reported platform total"),
        }

        for key, g in acc_gmv.items():
            meta = self.L2[key]
            origin = "residual" if key in residual else self._origins.get(key, "direct")
            prov, conf, how = meta_prov[origin]
            u = acc_units.get(key)
            a = (g * 10.0 / u) if u and u > 0 else None
            src = (f"MIS {month_label(self.month)} category GMV"
                   if origin in ("direct", "merge", "split")
                   else f"{self.ref.model_sheet} {month_label(self.prior)} share")
            self.out.l2[key] = {
                "gmv": Value(g, prov, "INR Cr", source=src, method=how, confidence=conf),
                "units": Value(
                    u, Provenance.DERIVED if origin != "residual" else Provenance.ESTIMATED,
                    "Mn", source=src,
                    method=("units = GMV x 10 / ASP" if origin in ("direct", "merge")
                            else f"units allocated on the {month_label(self.prior)} unit mix"),
                    confidence=conf) if u is not None
                else null("Mn", "no MIS ASP for this category"),
                "asp": Value(
                    a,
                    Provenance.REPORTED if origin == "direct" else
                    (Provenance.ESTIMATED if origin == "residual" else Provenance.DERIVED),
                    "INR", source=src,
                    method=("MIS reported category ASP" if origin == "direct"
                            else "ASP = allocated GMV x 10 / allocated units"),
                    confidence=conf) if a is not None
                else null("INR", "no MIS ASP for this category"),
            }
            self.out.set(meta["gmv"], self.out.l2[key]["gmv"])
            self.out.set(meta["units"], self.out.l2[key]["units"])
            self.out.set(meta["asp"], self.out.l2[key]["asp"])

    def _carve_residual(self, acc_gmv: dict[str, float],
                        acc_units: dict[str, float]) -> set[str]:
        """Hold back the model L2s the MIS has no line for (decision Q1A).

        The MIS 17 categories are the whole platform, so Watches and Fashion
        Accessories are carved out of that total at their historical share and
        the mapped categories scale down to keep the platform total intact.
        """
        res = self.cfg.get("residual_l2", {})
        keys = [k for k in res.get("keys", []) if k in self.L2]
        if not keys or res.get("policy") != "carve_from_total":
            return set()
        mapped_total = sum(acc_gmv.values())
        if mapped_total <= 0:
            return set()

        hist_prior_total = sum(
            self.ref.value(self.L2[k]["gmv"], self.prior) or 0.0 for k in self.L2)
        hist_res = sum(self.ref.value(self.L2[k]["gmv"], self.prior) or 0.0 for k in keys)
        if hist_prior_total <= 0 or hist_res <= 0:
            self.out.finding("RESIDUAL_NO_BASIS", Severity.WARNING,
                             f"No historical basis to carve {', '.join(keys)} out of the MIS "
                             "total; they are emitted as unavailable.")
            for k in keys:
                self.out.l2[k] = {"gmv": null("INR Cr", "no MIS line and no historical share"),
                                  "units": null("Mn"), "asp": null("INR")}
            return set()

        # GMV and units need their own shares. These categories carry a much
        # higher ASP than the book average, so their share of units is roughly
        # half their share of GMV; using one share for both would halve their ASP.
        share = hist_res / hist_prior_total
        hist_units_total = sum(
            self.ref.value(self.L2[k]["units"], self.prior) or 0.0 for k in self.L2)
        hist_res_units = sum(
            self.ref.value(self.L2[k]["units"], self.prior) or 0.0 for k in keys)
        unit_share = (hist_res_units / hist_units_total) if hist_units_total > 0 else share

        # Mapped categories scale GMV and units by the same factor, so their ASP
        # is left exactly as the MIS reported it. The residual categories take
        # their own, much lower, share of units, which is what restores their ASP.
        mapped_units = sum(acc_units.values())
        for k in list(acc_gmv):
            acc_gmv[k] *= (1.0 - share)
        for k in list(acc_units):
            acc_units[k] *= (1.0 - share)

        res_gmv_total = mapped_total * share
        res_units_total = mapped_units * unit_share
        hist_g = {k: self.ref.value(self.L2[k]["gmv"], self.prior) or 0.0 for k in keys}
        hist_u = {k: self.ref.value(self.L2[k]["units"], self.prior) or 0.0 for k in keys}
        for k, g in est.ratio_split(res_gmv_total, hist_g).items():
            acc_gmv[k] = g
        if sum(hist_u.values()) > 0 and res_units_total > 0:
            for k, u in est.ratio_split(res_units_total, hist_u).items():
                acc_units[k] = u

        self.out.adjustments.append(Adjustment(
            metric="category mix",
            driver="taxonomy coverage",
            before=mapped_total, after=mapped_total,
            rationale=(
                f"{', '.join(self.L2[k]['label'] for k in keys)} have no MIS line. Per the agreed "
                f"scope decision the MIS total is the whole platform, so they were carved out at "
                f"their {month_label(self.prior)} share of {share:.2%} "
                f"({res_gmv_total:,.1f} Cr) and the mapped categories scaled by "
                f"{1 - share:.4f}. Units were carved at their own "
                f"{unit_share:.2%} share, which is lower because these categories carry a "
                "higher ASP than the book average. The platform total is unchanged."),
            source="player configuration: residual_l2",
        ))
        self.out.finding(
            "RESIDUAL_CARVED", Severity.INFO,
            f"{', '.join(self.L2[k]['label'] for k in keys)} estimated at "
            f"{res_gmv_total:,.1f} Cr ({share:.2%} of platform) from the historical share.",
            "No MIS evidence exists for these categories; they are model estimates inside a "
            "reported total.")
        for k in keys:
            self.out.l2.setdefault(k, {})
        return set(keys)

    def expert_gaps(self) -> dict[str, dict]:
        """How far this month's output is from the call's category YoY, per category.

        gap = (1 + call YoY) / (1 + output YoY) - 1, so +5% means the expert sees the category
        5% higher than the output does. `estimated` is True when the output category was not
        reported by the MIS, which makes the gap the model's own error rather than the
        expert's difference from a reported actual.
        """
        if not self.tr or not self.tr.category_yoy:
            return {}
        if not self.has_categories:
            return self._expert_gaps_generic()
        aliases = self.cfg.get("call_category_aliases", {})
        tmap = self.cfg.get("mis_taxonomy_map") or {}
        labels = {m["label"].lower(): k for k, m in self.L2.items()}
        out: dict[str, dict] = {}
        for cat, yoy in self.tr.category_yoy.items():
            name = aliases.get(cat, cat)
            targets = [t for t in ((tmap.get(name) or {}).get("targets") or
                                   ([labels[name.lower()]] if name.lower() in labels else []))
                       if t in self.L2 and self.out.l2.get(t, {}).get("gmv") is not None
                       and self.out.l2[t]["gmv"].value]
            if not targets:
                continue
            now = sum(self.out.l2[t]["gmv"].value for t in targets)
            base = sum(self.ref.value(self.L2[t]["gmv"], self.yoy_month) or 0.0 for t in targets)
            if base <= 0 or now <= 0:
                continue
            gap = (1 + yoy) / (now / base) - 1
            est_only = all(not self.out.l2[t]["gmv"].provenance.is_actual for t in targets)
            for t in targets:
                out[t] = {"gap": gap, "estimated": est_only, "label": cat, "call_yoy": yoy,
                          "out_yoy": now / base - 1}
        return out

    def _expert_gaps_generic(self) -> dict[str, dict]:
        """expert_gaps for a player on the standard template (categories are share parameters)."""
        leaves = getattr(self, "_generic_leaves", None) or []
        reported = getattr(self.out, "cat_reported", set())
        out: dict[str, dict] = {}
        for cat, yoy in self.tr.category_yoy.items():
            name = cat.lower()
            lf = next((x for x in leaves if x["label"].lower() == name), None) or next(
                (x for x in leaves if name in x["label"].lower() or x["label"].lower() in name), None)
            if lf is None:
                continue
            now = self.out.num(self._par_row(lf["gmv"]))
            base = self.ref.value(self._par_row(lf["gmv"]), self.yoy_month)
            if not now or not base or base <= 0:
                continue
            out[f"par{lf['share']}"] = {"gap": (1 + yoy) / (now / base) - 1,
                                        "estimated": lf["share"] not in reported,
                                        "label": cat, "call_yoy": yoy, "out_yoy": now / base - 1}
        return out

    def _prior_call_tilt(self) -> dict[str, float]:
        """Category factors from the last known call, for a month that has no call of its own.

        A category growing faster than the platform on that call is assumed to keep doing so,
        by half as much, within +/-15%. The factor changes the mix only: the mix is rescaled
        onto the platform total afterwards, so no level is set by an old call.
        """
        pc = self.prior_call
        if pc is None or not pc.category_yoy or not pc.month:
            return {}
        cfg = self.cfg.get("prior_call") or {}
        damp, cap = cfg.get("damping", 0.5), cfg.get("max_tilt", 0.15)
        aliases = self.cfg.get("call_category_aliases", {})
        tmap = self.cfg.get("mis_taxonomy_map") or {}
        labels = {m["label"].lower(): k for k, m in self.L2.items()}
        g_now = self.ref.value(self.P["gross_gmv"], pc.month) if self.P.has("gross_gmv") else None
        g_ago = (self.ref.value(self.P["gross_gmv"], shift_months(pc.month, -12))
                 if self.P.has("gross_gmv") else None)
        plat = (g_now / g_ago - 1) if g_now and g_ago else \
            sum(pc.category_yoy.values()) / len(pc.category_yoy)
        out: dict[str, float] = {}
        for cat, yoy in pc.category_yoy.items():
            name = aliases.get(cat, cat)
            targets = (tmap.get(name) or {}).get("targets") or \
                ([labels[name.lower()]] if name.lower() in labels else [])
            if not targets or plat <= -0.99:
                continue
            rel = (1 + yoy) / (1 + plat)
            f = max(1 - cap, min(1 + cap, rel ** damp))
            for t in targets:
                if t in self.L2:
                    out[t] = f
        if out:
            self.out.finding(
                "PRIOR_CALL_TILT", Severity.INFO,
                f"Category mix tilted for {len(out)} categories using the last known call "
                f"({month_label(pc.month)}).",
                "Factors: " + ", ".join(f"{self.L2[k]['label']} x{v:.2f}" for k, v in
                                        list(out.items())[:8]) +
                f". Damping {damp:.0%}, cap +/-{cap:.0%}; the platform total is unchanged.")
        return out

    def _l2_from_history(self) -> None:
        """Fallback mix when no MIS category data exists.

        Each category is estimated from its own history, so the parts will not
        add up to the platform total on their own. `_rescale_l2_to_total` fixes
        that once the platform level is known, keeping history as the source of
        the *mix* and the platform figure as the source of the *level*.
        """
        self._l2_estimated_from_history = True
        tilt = self._prior_call_tilt()
        big = {k: v for k, v in self.learned.items() if abs(v) >= 0.005 and k in self.L2}
        if big:
            self.out.finding(
                "LEARNED_CORRECTION", Severity.INFO,
                f"Category mix corrected for {len(big)} categories from earlier months' gaps.",
                "Corrections: " + ", ".join(f"{self.L2[k]['label']} {v:+.1%}" for k, v in
                                            sorted(big.items(), key=lambda x: -abs(x[1]))[:8]) +
                ". Learned from how far past history-only estimates were from the reported MIS "
                "and the expert call; the platform total is unchanged.")
        for key, meta in self.L2.items():
            gmv = self._estimate_row(meta["gmv"], f"{meta['label']} shipped GMV", "INR Cr")
            ft, fl = tilt.get(key) or 1.0, 1.0 + self.learned.get(key, 0.0)
            f = ft * fl
            if abs(f - 1) > 1e-9 and gmv.value is not None:
                why = []
                if abs(ft - 1) > 1e-9:
                    why.append(f"x{ft:.3f} for category direction in the last known call "
                               f"({month_label(self.prior_call.month)})")
                if abs(fl - 1) > 1e-9:
                    why.append(f"x{fl:.3f} learned from earlier months' gap to the reported "
                               "and expert figures")
                gmv = Value(gmv.value * f, gmv.provenance, gmv.unit, source=gmv.source,
                            method=f"{gmv.method}; " + "; ".join(why),
                            confidence=gmv.confidence)
            asp = self._estimate_row(meta["asp"], f"{meta['label']} ASP", "INR")
            pa_ = self._prior(meta["asp"])
            if asp.value is not None and pa_ and not asp.is_null:       # a price does not jump: within 10% of last month
                lim = min(max(asp.value, pa_ * 0.90), pa_ * 1.10)
                if abs(lim - asp.value) > 1e-9:
                    asp = Value(lim, asp.provenance, asp.unit, source=asp.source,
                                method=f"{asp.method}; held within 10% of last month's ASP (trend guard)",
                                confidence=asp.confidence)
            u = (gmv.value * 10.0 / asp.value) if gmv.value is not None and asp.value else None
            units = (Value(u, Provenance.DERIVED, "Mn",
                           method="units = GMV x 10 / ASP", confidence=Confidence.LOW)
                     if u is not None else null("Mn", "no estimated ASP for this category"))
            # Recorded in out.l2 as well as on the sheet rows: the rescale, the
            # segment rollups and the share calculations all read from out.l2.
            self.out.l2[key] = {"gmv": gmv, "asp": asp, "units": units}
            self.out.set(meta["gmv"], gmv)
            self.out.set(meta["asp"], asp)
            self.out.set(meta["units"], units)

    # --------------------------------------------------- 2. platform topline

    def _step_platform_level(self) -> None:
        P = self.P
        secs = self.cfg.get("mis_sections", {})
        names = self.cfg.get("mis_metric_names", {})

        # Only a MIS-derived category sum may anchor the platform level. A sum of
        # history-based estimates is not a reported actual and must not pose as one.
        l2_total = 0.0
        if not self._l2_estimated_from_history:
            l2_total = sum(v["gmv"].value for v in self.out.l2.values()
                           if v.get("gmv") and v["gmv"].value is not None)

        shipped_mis = gross_mis = None
        if self.mis and self.month in self.mis.months:
            shipped_mis = self.mis.value(self.month, names.get("shipped_gmv", "Shipped GMV"),
                                         secs.get("overall", "Overall"))
            gross_mis = self.mis.value(self.month, names.get("gross_gmv", "Gross GMV at SP"),
                                       secs.get("overall", "Overall"))
        if self.gm is not None:
            # The standard template: zeros are "not supplied" and GMV is rescaled to Cr.
            shipped_mis = self.gm.overall.get("shipped_gmv")
            gross_mis = self.gm.overall.get("gross_gmv")
        # A platform cannot report zero GMV, so a zero means the cell was not filled in.
        shipped_mis = (shipped_mis / self.mis_scale) if (shipped_mis and self.gm is None) else (
            shipped_mis or None)
        gross_mis = (gross_mis / self.mis_scale) if (gross_mis and self.gm is None) else (
            gross_mis or None)

        # No MIS figure but the expert stated the platform GMV: that is better evidence than
        # any projection from history.
        if gross_mis is None and shipped_mis is None and l2_total <= 0 and self.tr is not None:
            self._call_stated_gmv = self.tr.stated_values.get("Platform GMV")

        # Players differ in which series their sheet stores. Myntra carries both
        # shipped and gross; most carry gross only. Anchor on whichever the MIS
        # reports, and derive the other from the cancellation rate.
        anchor = self.cfg.get("gmv_anchor", "auto")
        if anchor == "auto":
            if shipped_mis is not None or l2_total > 0:
                anchor = "shipped"
            elif gross_mis is not None or self._call_stated_gmv:
                anchor = "gross"
            else:
                # Growth settings act on gross GMV, so they pull a player onto it.
                anchor = ("shipped" if self.P.has("shipped_gmv") and not self.growth_override
                          else "gross")
        self.anchor = anchor

        if anchor == "gross" and l2_total <= 0:
            self._anchor_on_gross(gross_mis)
            return

        if l2_total > 0:
            v = Value(l2_total, Provenance.REPORTED, "INR Cr",
                      source=f"MIS {month_label(self.month)} category GMV",
                      method="sum of MIS category GMV mapped to the model taxonomy",
                      confidence=Confidence.HIGH)
            if shipped_mis is not None and abs(l2_total / shipped_mis - 1) > 0.002:
                v.with_note(
                    f"MIS states {shipped_mis:,.1f}; the category sum of {l2_total:,.1f} is used "
                    "so the category build reconciles exactly to the platform total.")
            self.out.set(P["shipped_gmv"], v)
        elif shipped_mis is not None:
            self.out.set(P["shipped_gmv"], Value(
                shipped_mis, Provenance.REPORTED, "INR Cr",
                source=f"MIS {month_label(self.month)}", method="MIS reported Shipped GMV",
                confidence=Confidence.HIGH))
        else:
            self.out.set(P["shipped_gmv"], self._estimate_gmv_history(
                P["shipped_gmv"], "Shipped GMV"))

        shipped = self.out.num(P["shipped_gmv"])
        self._plausibility("shipped_gmv", P["shipped_gmv"], shipped, "Shipped GMV")

        # Cancellation has no MIS line on Myntra's MIS (Gross GMV at SP is blank), so it is
        # estimated there; the standard template supplies it.
        canc = self._cancellation_value()
        self.out.set(P["cancellation_pct"], canc)
        self._plausibility("cancellation_pct", P["cancellation_pct"], canc.value, "Cancellation %")

        if shipped is not None and canc.value is not None and canc.value < 1:
            gross = shipped / (1 - canc.value)
            gv = Value(
                gross, Provenance.DERIVED, "INR Cr",
                source="Shipped GMV (MIS) and estimated cancellation",
                method="Gross GMV = Shipped GMV / (1 - cancellation %)",
                confidence=(Confidence.LOW if self.out.get(P["shipped_gmv"]).confidence
                            is Confidence.LOW else Confidence.MEDIUM))
            if self.growth_override:
                # Growth is set on gross GMV for every player, so gross is moved first and
                # shipped GMV and the categories follow it.
                bal = self._balance_gross(gv)
                if abs(bal.value / gv.value - 1) > 1e-9:
                    self._follow_gross(bal.value / gv.value)
                gv = bal
            self.out.set(P["gross_gmv"], gv)
            mis_gross = (self.mis.value(self.month, names.get("gross_gmv", "Gross GMV at SP"),
                                        secs.get("overall", "Overall"))
                         if self.mis and self.month in self.mis.months else None)
            if mis_gross is None:
                self.out.notes.append(
                    "The MIS leaves Gross GMV at SP blank, so cancellation could not be "
                    "back-solved from reported data and is a model estimate.")
        else:
            self.out.set(P["gross_gmv"], null("INR Cr", "shipped GMV or cancellation unavailable"))

    def _step_secondary(self) -> None:
        """Move gross GMV for news on the brand, by the weight given to secondary data.

        The wording of each item (or its explicit impact) gives a direction. The effect is that
        direction times the largest allowed move, scaled by how much of a full weight the
        secondary source carries, and capped. Shipped GMV and the categories follow.
        """
        sec = self.secondary
        if sec is None or not sec.items:
            return
        sc = self.cfg.get("secondary") or {}
        max_eff, full_w = sc.get("max_effect", 0.05), sc.get("full_weight", 0.05)
        w = self.weights.get("secondary", 0.0)
        n = len(sec.usable)
        if w <= 0:
            self.out.finding(
                "SECONDARY_NO_WEIGHT", Severity.WARNING,
                f"A secondary file with {len(sec.items)} items was supplied but its weight is 0%, "
                "so it did not change the output.",
                "Give 'secondary' a weight on the Weights tab to bring it into the numbers.")
            return
        d = sec.direction()
        e = d if sec.explicit else d * max_eff
        e = max(-max_eff, min(max_eff, e * min(1.0, w / full_w)))
        gv = self.out.get(self.P["gross_gmv"])
        if abs(e) < 1e-4 or gv is None or gv.value is None:
            self.out.finding("SECONDARY_NEUTRAL", Severity.INFO,
                             f"Secondary data ({n} scored items) was neutral, so the output is unchanged.")
            return
        k = 1 + e
        top = [i for i in sec.usable if (i.impact if i.impact is not None else i.score) * e > 0][:3]
        why = (f"{n} scored items ({'explicit impact' if sec.explicit else 'wording'}), net "
               f"{'positive' if e > 0 else 'negative'}; effective weight {w:.0%}; "
               f"gross GMV moved {e:+.1%} (cap {max_eff:.0%}). Examples: "
               + " | ".join(i.text[:90] for i in top))
        self.out.set(self.P["gross_gmv"], Value(
            gv.value * k, Provenance.CALIBRATED, "INR Cr", source=gv.source,
            method=f"{gv.method}; secondary data {e:+.1%}", confidence=gv.confidence,
            uncalibrated=gv.uncalibrated if gv.uncalibrated is not None else gv.value))
        self._follow_gross(k)
        self.out.adjustments.append(Adjustment(
            metric="Gross GMV", driver="secondary data", before=gv.value, after=gv.value * k,
            rationale=why, source="secondary file (news on the brand)"))
        self.out.finding("SECONDARY_APPLIED", Severity.INFO,
                         f"Secondary data moved gross GMV {e:+.1%} to {gv.value * k:,.1f} Cr.", why,
                         metric="Gross GMV")

    def _follow_gross(self, k: float) -> None:
        """Scale shipped GMV and the category build by k when gross GMV has been moved."""
        P = self.P
        cur = self.out.get(P["shipped_gmv"])
        why = f"scaled by {k:.4f} to follow the gross GMV growth setting"
        if cur is not None and cur.value is not None:
            self.out.set(P["shipped_gmv"], Value(
                cur.value * k, Provenance.CALIBRATED, "INR Cr", source=cur.source,
                method=f"{cur.method}; {why}", confidence=Confidence.MEDIUM,
                uncalibrated=cur.value))
        for key, meta in self.L2.items():
            d = self.out.l2.get(key, {})
            g, u = d.get("gmv"), d.get("units")
            if g is not None and g.value is not None:
                d["gmv"] = Value(g.value * k, Provenance.CALIBRATED, "INR Cr", source=g.source,
                                 method=f"{g.method}; {why}", confidence=Confidence.MEDIUM,
                                 uncalibrated=g.value)
                self.out.set(meta["gmv"], d["gmv"])
            if u is not None and u.value is not None:
                d["units"] = Value(u.value * k, Provenance.CALIBRATED, "Mn",
                                   method=f"{u.method}; scaled with GMV",
                                   confidence=Confidence.MEDIUM, uncalibrated=u.value)
                self.out.set(meta["units"], d["units"])

    def _anchor_on_gross(self, gross_mis: float | None) -> None:
        """Gross GMV is the primary series; shipped follows from cancellation.

        This is the path for every player whose sheet stores par 241 but not
        par 243, which is all of them except Myntra and Nykaa Fashion.
        """
        P = self.P
        if gross_mis is not None:
            self.out.set(P["gross_gmv"], self._balance_gross(Value(
                gross_mis, Provenance.REPORTED, "INR Cr",
                source=f"MIS {month_label(self.month)}",
                method="MIS reported Gross GMV", confidence=Confidence.HIGH)))
        else:
            stated = self._call_stated_gmv
            if stated:
                v = Value(stated, Provenance.CALL_STATED, "INR Cr",
                          source="expert call, confirmed by the interviewer",
                          method="platform GMV stated on the call; no MIS supplied",
                          confidence=Confidence.MEDIUM)
                self.out.finding(
                    "GMV_FROM_CALL", Severity.INFO,
                    f"Gross GMV of {stated:,.1f} Cr is the figure stated on the call.",
                    "No MIS was supplied. The call's figure is spoken and rounded, so it is "
                    "used in preference to a history-based estimate but ranks below a MIS.",
                    metric="gross_gmv")
            else:
                v = self._estimate_gmv_history(P["gross_gmv"], "Gross GMV")
            self.out.set(P["gross_gmv"], self._balance_gross(v))

        gross = self.out.num(P["gross_gmv"])
        self._plausibility("gross_gmv", P["gross_gmv"], gross, "Gross GMV")

        canc = self._cancellation_value()
        self.out.set(P["cancellation_pct"], canc)
        self._plausibility("cancellation_pct", P["cancellation_pct"], canc.value,
                           "Cancellation %")

        if gross is not None and canc.value is not None:
            self.out.set(P["shipped_gmv"], Value(
                gross * (1 - canc.value), Provenance.DERIVED, "INR Cr",
                source="Gross GMV and estimated cancellation",
                method="Shipped GMV = Gross GMV x (1 - cancellation %)",
                confidence=Confidence.MEDIUM))
        else:
            self.out.set(P["shipped_gmv"], null(
                "INR Cr", "gross GMV or cancellation unavailable"))

    # --------------------------------------------------- 3. returns and derived

    def _rescale_l2_to_total(self) -> None:
        """Scale a history-derived category mix onto the platform total."""
        if not getattr(self, "_l2_estimated_from_history", False):
            return
        shipped = self.out.num(self.P["shipped_gmv"])
        total = sum(v["gmv"].value for v in self.out.l2.values()
                    if v.get("gmv") and v["gmv"].value is not None)
        if not shipped or total <= 0:
            return
        k = shipped / total
        # Trend guard: a category's share of the platform moves only gradually month to month, so each share stays
        # within +/-12% of last month's and the rest absorb the difference. This stops a spike or dip in one
        # category's own history from showing up as a wild month-on-month move.
        prior_ship = self.ref.value(self.P["shipped_gmv"], self.prior)
        final: dict[str, float] = {}
        for key, meta in self.L2.items():
            cur = self.out.l2.get(key, {}).get("gmv")
            if cur is not None and cur.value is not None:
                final[key] = cur.value * k
        held: set[str] = set()
        if prior_ship:
            for key, x in list(final.items()):
                pv = self.ref.value(self.L2[key]["gmv"], self.prior)
                if pv and pv > 0:
                    ps = pv / prior_ship
                    lo, hi = ps * 0.88 * shipped, ps * 1.12 * shipped
                    if x < lo or x > hi:
                        final[key], held = min(max(x, lo), hi), held | {key}
            free = {kk: v for kk, v in final.items() if kk not in held}
            gap = shipped - sum(final.values())
            if free and sum(free.values()) > 0 and abs(gap) > 1e-9:
                f = 1 + gap / sum(free.values())
                if f > 0:
                    final = {kk: (v if kk in held else v * f) for kk, v in final.items()}
        for key, meta in self.L2.items():
            cur = self.out.l2.get(key, {}).get("gmv")
            if cur is None or cur.value is None:
                continue
            kk = final[key] / cur.value if cur.value else k
            scaled = Value(
                final[key], Provenance.ESTIMATED, "INR Cr",
                source=cur.source,
                method=f"{cur.method}; scaled by {k:.4f} so the categories sum to the "
                       "platform total"
                       + ("; share held within 12% of last month's (trend guard)" if key in held else ""),
                confidence=Confidence.LOW)
            self.out.l2[key]["gmv"] = scaled
            self.out.set(meta["gmv"], scaled)
            u = self.out.l2[key].get("units")
            if u is not None and u.value is not None:
                scaled_u = Value(u.value * kk, Provenance.ESTIMATED, "Mn",
                                 method=f"{u.method}; scaled with GMV",
                                 confidence=Confidence.LOW)
                self.out.l2[key]["units"] = scaled_u
                self.out.set(meta["units"], scaled_u)
        self.out.finding(
            "L2_MIX_FROM_HISTORY", Severity.INFO,
            f"Category mix taken from history and scaled by {k:.4f} onto the platform total.",
            "No MIS category data was supplied, so history supplies the mix and the platform "
            "figure supplies the level. These categories are estimates, not reported values.")

    def _step_returns_and_derived(self) -> None:
        self._rescale_l2_to_total()
        P = self.P
        secs = self.cfg.get("mis_sections", {})
        names = self.cfg.get("mis_metric_names", {})

        ret = None
        if self.mis and self.month in self.mis.months:
            ret = self.mis.value(self.month, names.get("returns", "Returns"),
                                 secs.get("overall", "Overall"))

        if ret is not None:
            self.out.set(P["returns_pct"], Value(
                ret, Provenance.REPORTED, "%",
                source=f"MIS {month_label(self.month)} Returns",
                method="MIS reported returns rate", confidence=Confidence.HIGH))
            self._plausibility("returns_pct", P["returns_pct"], ret, "Returns %")
            # The model splits returns into RTO and RVP; the MIS gives only the total.
            h_rto = self._prior(P["rto_pct"]) or 0.0
            h_rvp = self._prior(P["rvp_pct"]) or 0.0
            if h_rto + h_rvp > 0:
                share = h_rto / (h_rto + h_rvp)
                for row, part, lbl in ((P["rto_pct"], share, "RTO"),
                                       (P["rvp_pct"], 1 - share, "RVP")):
                    self.out.set(row, Value(
                        ret * part, Provenance.DERIVED, "%",
                        source="MIS returns total split on the prior-month RTO:RVP ratio",
                        method=f"{lbl} = MIS returns x {part:.4f} "
                               f"({month_label(self.prior)} split)",
                        confidence=Confidence.MEDIUM))
            else:
                for row in (P["rto_pct"], P["rvp_pct"]):
                    self.out.set(row, null("%", "no prior RTO:RVP ratio to split the MIS total"))
        else:
            mis_rr = {"rto_pct": self.gm.overall.get("rto_pct") if self.gm else None,
                      "rvp_pct": self.gm.overall.get("rvp_pct") if self.gm else None}
            for key, lbl in (("rto_pct", "RTO %"), ("rvp_pct", "RVP %")):
                row, mv = P[key], mis_rr[key]
                if mv is not None:
                    self.out.set(row, Value(
                        mv, Provenance.REPORTED, "%", source=f"MIS {month_label(self.month)}",
                        method=f"MIS reported {lbl}", confidence=Confidence.HIGH))
                else:
                    v = self._estimate_row(row, lbl, "%", clip_tolerance=0.10)
                    if self.gm is not None:
                        v.with_note("The MIS left this blank or zero, so history supplies it.")
                    self.out.set(row, v)
            rto, rvp = self.out.num(P["rto_pct"]), self.out.num(P["rvp_pct"])
            self.out.set(P["returns_pct"], Value(
                (rto + rvp) if rto is not None and rvp is not None else None,
                Provenance.DERIVED, "%", method="Returns % = RTO % + RVP %",
                confidence=Confidence.LOW))

        shipped = self.out.num(P["shipped_gmv"])
        rpct = self.out.num(P["returns_pct"])
        self.out.set(P["net_gmv"], Value(
            shipped * (1 - rpct) if shipped is not None and rpct is not None else None,
            Provenance.DERIVED, "INR Cr",
            method="Net GMV = Shipped GMV x (1 - returns %)",
            confidence=Confidence.MEDIUM) if shipped is not None and rpct is not None
            else null("INR Cr", "shipped GMV or returns unavailable"))

    # ---------------------------------------------------- 4. units, ASP, AOV

    def _step_units_and_aov(self) -> None:
        P = self.P
        units = sum(v["units"].value for v in self.out.l2.values()
                    if v.get("units") and v["units"].value is not None)
        shipped = self.out.num(P["shipped_gmv"])

        mis_asp = self.gm.overall.get("asp") if self.gm else None
        if mis_asp:
            asp = mis_asp
            self.out.set(P["asp"], Value(
                mis_asp, Provenance.REPORTED, "INR", source=f"MIS {month_label(self.month)}",
                method="MIS reported Platform ASP", confidence=Confidence.HIGH))
        elif units > 0 and shipped is not None:
            asp = shipped * 10.0 / units
            self.out.set(P["asp"], Value(
                asp, Provenance.DERIVED, "INR",
                source="MIS category GMV and ASP",
                method="ASP = Shipped GMV x 10 / total shipped units, units summed from the "
                       "MIS category GMV/ASP pairs",
                confidence=Confidence.HIGH))
        else:
            self.out.set(P["asp"], self._estimate_row(P["asp"], "ASP", "INR"))
            asp = self.out.num(P["asp"])

        # Methodology change: the reference workbook carries AOV forward with a
        # hardcoded multiplier and derives basket size from it. Basket size is the
        # more stable series, so it is estimated and AOV derived from the
        # MIS-grounded ASP instead.
        basket = self._estimate_row(P["basket_size"], "Basket size", "#", clip_tolerance=0.12)
        self.out.set(P["basket_size"], basket)
        self._plausibility("basket_size", P["basket_size"], basket.value, "Basket size")

        # A MIS AOV that agrees with the model's own definition (within the configured
        # band of the history-based figure) is a reported actual. One that does not is
        # a different unit and is recorded, not used.
        mis_aov = self.gm.overall.get("aov") if self.gm else None
        lo, hi = (self.cfg.get("mis_generic") or {}).get("aov_tolerance", [0.65, 1.35])
        hist_aov = self._estimate_row(P["aov"], "AOV", "INR") if mis_aov else None
        accept_aov = bool(mis_aov and hist_aov is not None and hist_aov.value
                          and lo <= mis_aov / hist_aov.value <= hi)

        if accept_aov:
            self.out.set(P["aov"], Value(
                mis_aov, Provenance.REPORTED, "INR", source=f"MIS {month_label(self.month)}",
                method=f"MIS reported AOV, within {lo:.0%}-{hi:.0%} of the history-based "
                       f"{hist_aov.value:,.0f}, so it is on the model's definition",
                confidence=Confidence.HIGH))
            if asp:
                self.out.set(P["basket_size"], Value(
                    mis_aov / asp, Provenance.DERIVED, "#",
                    method="Basket size = AOV / ASP", confidence=Confidence.MEDIUM))
        elif asp is not None and basket.value is not None:
            aov = asp * basket.value
            prev_formula = self.ref.formulas.get(P["aov"], {}).get(self.month, "")
            self.out.set(P["aov"], Value(
                aov, Provenance.DERIVED, "INR",
                source="MIS-derived ASP x estimated basket size",
                method="AOV = ASP x basket size",
                confidence=Confidence.MEDIUM))
            self.out.method_changes.append({
                "metric": "AOV (par 240)",
                "was": prev_formula or "carried forward with a fixed multiplier",
                "now": "AOV = ASP x basket size, with ASP grounded in MIS category GMV/ASP "
                       "and basket size estimated from its own history",
                "why": "The reference formula was an unevidenced carry-forward. Anchoring AOV to "
                       "reported category data removes a free parameter and keeps AOV consistent "
                       "with the reported category mix.",
            })
        else:
            self.out.set(P["aov"], self._estimate_row(P["aov"], "AOV", "INR"))

        gross, aovv = self.out.num(P["gross_gmv"]), self.out.num(P["aov"])
        self.out.set(P["gross_transactions"], Value(
            gross * 10.0 / aovv, Provenance.DERIVED, "Mn",
            method="Gross transactions = Gross GMV x 10 / AOV",
            confidence=Confidence.MEDIUM)
            if gross is not None and aovv else null("Mn", "gross GMV or AOV unavailable"))

        self._record_mis_definition_conflicts()

    def _record_mis_definition_conflicts(self) -> None:
        """Capture MIS metrics whose unit definition conflicts with the model.

        Decision Q2A: these are logged, never applied. Writing them into the
        model would silently break every metric derived from them.
        """
        if not self.mis or self.month not in self.mis.months:
            return
        secs = self.cfg.get("mis_sections", {})
        conflicts = self.cfg.get("mis_definition_conflicts", {})
        for mis_name, spec in conflicts.items():
            if mis_name.startswith("_"):
                continue
            cell = self.mis.get(self.month, mis_name, secs.get("overall", "Overall"))
            if cell is None or cell.value is None:
                continue
            row = self.P.get(spec["model_metric"])
            model_val = self.out.num(row) if row else None
            if model_val in (None, 0):
                continue
            ratio = cell.value / model_val
            if self.gm is not None and spec["model_metric"] == "aov" \
                    and 0.65 <= ratio <= 1.35:
                continue                 # accepted as reported; nothing to flag
            self.out.finding(
                "MIS_DEFINITION_CONFLICT", Severity.WARNING,
                f"MIS '{mis_name}' = {cell.value:,.4g} against model "
                f"{spec['model_metric']} = {model_val:,.4g} ({ratio:.2f}x).",
                f"{spec['note']} Recorded for the audit trail; not used numerically, because "
                "every metric downstream of it is derived on the model's definition.",
                metric=spec["model_metric"])

    # ----------------------------------------------------------- 5. user base

    def _step_users(self) -> None:
        P = self.P
        has_traffic = self.weights.get("traffic", 0) > 0
        mau = self._estimate_row(P["mau"], "MAU", "Mn", clip_tolerance=0.10)
        if not has_traffic:
            mau.confidence = Confidence.LOW
            mau.with_note("No traffic data supplied; MAU rests on its own history alone.")
        self.out.set(P["mau"], mau)

        mac = self._estimate_row(P["mac"], "MAC (MTU/MAU)", "%", clip_tolerance=0.08)
        self.out.set(P["mac"], mac)

        mtu = (mau.value * mac.value) if mau.value is not None and mac.value is not None else None
        self.out.set(P["mtu"], Value(
            mtu, Provenance.ESTIMATED, "Mn",
            source="estimated MAU and MAC",
            method="MTU = MAU x MAC", confidence=Confidence.LOW)
            if mtu is not None else null("Mn", "MAU or MAC unavailable"))
        for src, dst in (("mtu", "mtu_dup"), ("mau", "unique_visitors")):
            v = self.out.get(P[src])
            if v is not None:
                self.out.set(P[dst], v)

        txn = self.out.num(P["gross_transactions"])
        self.out.set(P["frequency"], Value(
            txn / mtu, Provenance.DERIVED, "#",
            method="Frequency = gross transactions / MTU", confidence=Confidence.LOW)
            if txn is not None and mtu else null("#", "transactions or MTU unavailable"))

    # ------------------------------------------------------------ 6. segments

    def _step_segments(self) -> None:
        if not self.has_categories:
            return
        by_seg: dict[str, list[str]] = {}
        for key, meta in self.L2.items():
            by_seg.setdefault(meta["segment"], []).append(key)

        canc_platform = self.out.num(self.P["cancellation_pct"])
        hist_w: dict[str, float] = {}
        for seg in by_seg:
            hist_w[seg] = self.ref.value(self.S[seg]["cancellation"], self.prior) or 0.0

        seg_gmv: dict[str, float] = {}
        for seg, keys in by_seg.items():
            rows = self.S[seg]
            g = sum(self.out.l2[k]["gmv"].value for k in keys
                    if k in self.out.l2 and self.out.l2[k].get("gmv")
                    and self.out.l2[k]["gmv"].value is not None)
            u = sum(self.out.l2[k]["units"].value for k in keys
                    if k in self.out.l2 and self.out.l2[k].get("units")
                    and self.out.l2[k]["units"].value is not None)
            seg_gmv[seg] = g
            self.out.set(rows["shipped_gmv"], Value(
                g, Provenance.DERIVED, "INR Cr",
                method=f"sum of the {len(keys)} mapped L2 categories",
                source="MIS category GMV", confidence=Confidence.HIGH))
            self.out.set(rows["items"], Value(
                u, Provenance.DERIVED, "Mn", method="sum of L2 shipped units",
                confidence=Confidence.HIGH) if u > 0 else null("Mn"))
            self.out.set(rows["asp"], Value(
                g * 10.0 / u, Provenance.DERIVED, "INR",
                method="segment ASP = segment GMV x 10 / segment units",
                confidence=Confidence.HIGH) if u > 0 else null("INR"))

        # Segment cancellations are scaled off the platform estimate so the
        # GMV-weighted average reconciles exactly to it.
        total_g = sum(seg_gmv.values())
        if canc_platform is not None and total_g > 0 and sum(hist_w.values()) > 0:
            hist_weighted = sum(hist_w[s] * seg_gmv[s] for s in seg_gmv) / total_g
            k = (canc_platform / hist_weighted) if hist_weighted else 1.0
            for seg in by_seg:
                self.out.set(self.S[seg]["cancellation"], Value(
                    hist_w[seg] * k, Provenance.DERIVED, "%",
                    method=f"prior-month segment cancellation x {k:.4f}, scaled so the "
                           "GMV-weighted average equals the platform estimate",
                    confidence=Confidence.LOW))
        else:
            for seg in by_seg:
                self.out.set(self.S[seg]["cancellation"],
                             self._estimate_row(self.S[seg]["cancellation"],
                                                f"{seg} cancellation %", "%"))

        for seg in by_seg:
            rows = self.S[seg]
            g, c = self.out.num(rows["shipped_gmv"]), self.out.num(rows["cancellation"])
            self.out.set(rows["gross_gmv"], Value(
                g / (1 - c), Provenance.DERIVED, "INR Cr",
                method="segment gross GMV = shipped / (1 - segment cancellation)",
                confidence=Confidence.MEDIUM)
                if g is not None and c is not None and c < 1 else null("INR Cr"))

            basket = self._estimate_row(rows["basket"], f"{seg} basket size", "#",
                                        clip_tolerance=0.12)
            self.out.set(rows["basket"], basket)
            a = self.out.num(rows["asp"])
            self.out.set(rows["aov"], Value(
                a * basket.value, Provenance.DERIVED, "INR",
                method="segment AOV = segment ASP x segment basket size",
                confidence=Confidence.MEDIUM)
                if a is not None and basket.value is not None else null("INR"))

            gg, aov = self.out.num(rows["gross_gmv"]), self.out.num(rows["aov"])
            self.out.set(rows["transactions"], Value(
                gg * 10.0 / aov, Provenance.DERIVED, "Mn",
                method="segment transactions = segment gross GMV x 10 / segment AOV",
                confidence=Confidence.MEDIUM)
                if gg is not None and aov else null("Mn"))

            self.out.set(rows["users"], self._estimate_row(
                rows["users"], f"{seg} users", "Mn", clip_tolerance=0.10))
            t, usr = self.out.num(rows["transactions"]), self.out.num(rows["users"])
            self.out.set(rows["frequency"], Value(
                t / usr, Provenance.DERIVED, "#",
                method="segment frequency = segment transactions / segment users",
                confidence=Confidence.LOW)
                if t is not None and usr else null("#"))

    # ------------------------------------------- 7. payment, geo, fulfilment

    def _step_splits(self) -> None:
        P = self.P
        secs = self.cfg.get("mis_sections", {})
        names = self.cfg.get("mis_metric_names", {})
        geo_sec = secs.get("geography", "Geographic Split")
        pay_sec = secs.get("payment", "Payment Split")

        geo = self.mis.section_values(self.month, geo_sec) if (
            self.mis and self.month in self.mis.months) else {}
        geo_map = (("metro", P["metro_pct"]), ("tier1", P["tier1_pct"]),
                   ("tier2plus", P["tier2plus_pct"]))
        reported = [k for k, _ in geo_map if names.get(k, k) in geo]

        if len(reported) == 3:
            for key, row in geo_map:
                nm = names.get(key, key)
                self.out.set(row, Value(
                    geo[nm], Provenance.REPORTED, "%",
                    source=f"MIS {month_label(self.month)} {geo_sec}",
                    method="MIS reported geographic split", confidence=Confidence.HIGH))
                self._plausibility(f"{key}_pct", row, geo[nm], f"{nm} share")
        else:
            # Estimate the two leading tiers and take the third as the residual,
            # as the reference model does, so the split always sums to 1.
            for key, row in geo_map[:2]:
                nm = names.get(key, key)
                if nm in geo:
                    self.out.set(row, Value(
                        geo[nm], Provenance.REPORTED, "%",
                        source=f"MIS {month_label(self.month)} {geo_sec}",
                        method="MIS reported geographic split", confidence=Confidence.HIGH))
                    self._plausibility(f"{key}_pct", row, geo[nm], f"{nm} share")
                else:
                    self.out.set(row, self._estimate_row(row, f"{nm} share", "%"))
            metro, tier1 = self.out.num(P["metro_pct"]), self.out.num(P["tier1_pct"])
            if metro is not None and tier1 is not None:
                if metro + tier1 >= 1:
                    self.out.finding(
                        "GEO_SHARES_EXCEED_ONE", Severity.WARNING,
                        f"Estimated Metro ({metro:.1%}) and Tier 1 ({tier1:.1%}) already "
                        "exceed 100%.",
                        "Both were rescaled to leave a non-negative Tier 2+ residual.",
                        metric="geo_split")
                    scale = 0.999 / (metro + tier1)
                    metro, tier1 = metro * scale, tier1 * scale
                    for row, v in ((P["metro_pct"], metro), (P["tier1_pct"], tier1)):
                        cur = self.out.get(row)
                        self.out.set(row, Value(
                            v, Provenance.ESTIMATED, "%", source=cur.source if cur else "",
                            method=(cur.method if cur else "") + "; rescaled to fit the split",
                            confidence=Confidence.LOW))
                self.out.set(P["tier2plus_pct"], Value(
                    1 - metro - tier1, Provenance.DERIVED, "%",
                    method="Tier 2+ % = 1 - Metro % - Tier 1 % (reference model formula)",
                    confidence=Confidence.MEDIUM))
            else:
                self.out.set(P["tier2plus_pct"], self._estimate_row(
                    P["tier2plus_pct"], "Tier 2+ share", "%"))

        pay = self.mis.section_values(self.month, pay_sec) if (
            self.mis and self.month in self.mis.months) else {}
        cod_name = names.get("cod", "COD (%)")
        if cod_name in pay:
            self.out.set(P["cod_pct"], Value(
                pay[cod_name], Provenance.REPORTED, "%",
                source=f"MIS {month_label(self.month)} {pay_sec}",
                method="MIS reported COD share", confidence=Confidence.HIGH))
        else:
            v = self._estimate_row(P["cod_pct"], "COD share", "%", clip_tolerance=0.08)
            v.with_note("The MIS payment-split rows are blank for this month.")
            self.out.set(P["cod_pct"], v)
        cod = self.out.num(P["cod_pct"])
        self.out.set(P["prepaid_pct"], Value(
            1 - cod, Provenance.DERIVED, "%", method="Prepaid % = 1 - COD %",
            confidence=Confidence.MEDIUM) if cod is not None else null("%"))

        # Segments inherit the platform splits, as the reference model does.
        for seg, rows in self.S.items():
            for src, dst in (("cod_pct", "cod"), ("prepaid_pct", "prepaid"),
                             ("metro_pct", "metro"), ("tier1_pct", "tier1"),
                             ("tier2plus_pct", "tier2plus"), ("rto_pct", "rto"),
                             ("rvp_pct", "rvp")):
                base = self.out.get(P[src])
                if base is None or base.is_null:
                    self.out.set(rows[dst], null("%"))
                    continue
                self.out.set(rows[dst], Value(
                    base.value, base.provenance, "%",
                    source=base.source,
                    method=f"segment inherits the platform {src.replace('_pct', '')} "
                           "(reference model treats these as platform-wide)",
                    confidence=base.confidence))

        has_logistics = self.weights.get("logistics", 0) > 0
        cap = self._estimate_row(P["captive_pct"], "Captive shipment share", "%",
                                 clip_tolerance=0.05)
        if not has_logistics:
            cap.confidence = Confidence.LOW
            cap.with_note("No logistics or shipped-volume file supplied.")
        self.out.set(P["captive_pct"], cap)
        c = self.out.num(P["captive_pct"])
        self.out.set(P["tpl_pct"], Value(
            1 - c, Provenance.DERIVED, "%", method="3PL % = 1 - captive %",
            confidence=Confidence.LOW) if c is not None else null("%"))
        shipped = self.out.num(P["shipped_gmv"])
        for row, pct_row, lbl in ((P["captive_gmv"], P["captive_pct"], "Captive"),
                                  (P["tpl_gmv"], P["tpl_pct"], "3PL")):
            p = self.out.num(pct_row)
            self.out.set(row, Value(
                shipped * p, Provenance.DERIVED, "INR Cr",
                method=f"{lbl} GMV = shipped GMV x {lbl} share", confidence=Confidence.LOW)
                if shipped is not None and p is not None else null("INR Cr"))

    # ------------------------------------------------------------- 8. shares

    def _step_shares(self) -> None:
        if not self.has_categories:
            return
        shipped = self.out.num(self.P["shipped_gmv"])
        if not shipped:
            return
        for key, meta in self.L2.items():
            g = self.out.num(meta["gmv"])
            self.out.set(meta["share"], Value(
                g / shipped, Provenance.DERIVED, "%",
                method="contribution share = L2 shipped GMV / platform shipped GMV",
                confidence=Confidence.HIGH) if g is not None else null("%"))
            seg_g = self.out.num(self.S[meta["segment"]]["shipped_gmv"])
            self.out.set(meta["pct_of_seg"], Value(
                g / seg_g, Provenance.DERIVED, "%",
                method="share of segment shipped GMV", confidence=Confidence.HIGH)
                if g is not None and seg_g else null("%"))
        for seg, rows in self.S.items():
            g = self.out.num(rows["shipped_gmv"])
            self.out.set(rows["contrib_share"], Value(
                g / shipped, Provenance.DERIVED, "%",
                method="segment share of platform shipped GMV",
                confidence=Confidence.HIGH) if g is not None else null("%"))
            gg = self.out.num(rows["gross_gmv"])
            self.out.set(rows["gross_contrib"], Value(
                gg / shipped, Provenance.DERIVED, "%",
                method="segment gross GMV / platform shipped GMV (as the reference model)",
                confidence=Confidence.MEDIUM) if gg is not None else null("%"))

    # ------------------------------------------------------------ 9. traffic

    def _step_traffic(self) -> None:
        P = self.P
        mtu, prev_mtu = self.out.num(P["mtu"]), self._prior(P["mtu"])
        mom = (mtu / prev_mtu - 1) if mtu is not None and prev_mtu else None
        self.out.set(P["mtu_mom_helper"], Value(
            mom, Provenance.DERIVED, "%", method="MTU month-on-month change",
            confidence=Confidence.LOW) if mom is not None else null("%"))

        app = self._carry(P["app_share"], "App share of sessions", "%")
        self.out.set(P["app_share"], app)
        a = self.out.num(P["app_share"])
        self.out.set(P["web_share"], Value(
            1 - a, Provenance.DERIVED, "%", method="Web share = 1 - app share",
            confidence=Confidence.LOW) if a is not None else null("%"))

        prev_sessions = self._prior(P["sessions"])
        self.out.set(P["sessions"], Value(
            prev_sessions * (1 + mom), Provenance.ESTIMATED, "Mn",
            source="prior-month sessions and MTU trend",
            method="sessions grow with MTU month-on-month (reference model behaviour)",
            confidence=Confidence.LOW)
            if prev_sessions is not None and mom is not None
            else null("Mn", "no traffic data and no prior-month sessions"))

        sessions = self.out.num(P["sessions"])
        mau = self.out.num(P["mau"])
        for row, share_row in ((P["app_mau"], P["app_share"]), (P["web_mau"], P["web_share"])):
            s = self.out.num(share_row)
            self.out.set(row, Value(
                s * mau, Provenance.DERIVED, "Mn", method="share x MAU",
                confidence=Confidence.LOW) if s is not None and mau is not None else null("Mn"))
        for row, share_row in ((P["app_sessions"], P["app_share"]),
                               (P["web_sessions"], P["web_share"])):
            s = self.out.num(share_row)
            self.out.set(row, Value(
                s * sessions, Provenance.DERIVED, "Mn", method="share x total sessions",
                confidence=Confidence.LOW)
                if s is not None and sessions is not None else null("Mn"))

        for row in (P["app_dau"], P["app_sessions_per_day"], P["app_time_per_session"],
                    P["web_dau"], P["web_sessions_per_day"], P["web_time_per_session"]):
            prev = self._prior(row)
            self.out.set(row, Value(
                prev * (1 + mom), Provenance.ESTIMATED, "",
                method="prior month scaled by the MTU month-on-month change "
                       "(reference model behaviour)",
                confidence=Confidence.LOW)
                if prev is not None and mom is not None else null())

        aov = self.out.num(P["aov"])
        self.out.set(P["avg_shipment_value"], Value(
            aov / 1.2, Provenance.DERIVED, "INR",
            method="Average shipment value = AOV / 1.2 (reference model constant)",
            confidence=Confidence.LOW) if aov is not None else null("INR"))
        aov_v = self.out.get(P["aov"])
        if aov_v is not None:
            self.out.set(P["aov_dup"], aov_v)

        prev_reg = self._prior(P["registered_users"])
        self.out.set(P["registered_users"], Value(
            prev_reg * 1.002, Provenance.ASSUMED, "Mn",
            method="prior month x 1.002 (reference model constant)",
            confidence=Confidence.LOW) if prev_reg is not None else null("Mn"))

        self.out.set(P["txn_app_pct"], self.out.rows.get(P["app_share"], null("%")))
        ap = self.out.num(P["txn_app_pct"])
        self.out.set(P["txn_new_pct"], Value(
            1 - ap, Provenance.DERIVED, "%", method="1 - app transaction share",
            confidence=Confidence.LOW) if ap is not None else null("%"))

    # --------------------------------------------------------------- 10. tail

    def _step_tail(self) -> None:
        P = self.P
        for key, lbl in (("paid_pct", "Paid share"), ("cac", "CAC"),
                         ("m1_retention", "M1 retention"), ("m3_retention", "M3 retention")):
            self.out.set(P[key], self._carry(P[key], lbl, "%" if key != "cac" else "INR"))
        paid = self.out.num(P["paid_pct"])
        self.out.set(P["organic_pct"], Value(
            1 - paid, Provenance.DERIVED, "%", method="Organic % = 1 - paid %",
            confidence=Confidence.LOW) if paid is not None else null("%"))

        # Rollups nest — Apparel sums Women/Men/Kids, which are themselves
        # rollups — so evaluate repeatedly until nothing more resolves rather
        # than trusting the order they appear in the configuration.
        rollups = {int(k): v for k, v in self.cfg.get("l2_rollups", {}).items()
                   if k.isdigit()}
        pending = dict(rollups)
        while pending:
            progressed = False
            for row, parts in list(pending.items()):
                total_g = 0.0
                ok = True
                for p in parts:
                    if p.isdigit():
                        if int(p) in pending:        # a component is still unresolved
                            ok = False
                            break
                        g = self.out.num(int(p))
                    else:
                        meta = self.L2.get(p)
                        g = self.out.num(meta["gmv"]) if meta else None
                    if g is None:
                        ok = False
                        break
                    total_g += g
                if not ok:
                    continue
                self.out.set(row, Value(
                    total_g, Provenance.DERIVED, "INR Cr",
                    source="sum of the categories beneath it",
                    method=f"rollup of {', '.join(parts)}", confidence=Confidence.HIGH))
                del pending[row]
                progressed = True
            if not progressed:
                break

        for row, parts in pending.items():
            self.out.set(row, null(
                "INR Cr", f"rollup of {', '.join(parts)}: one or more components unavailable"))
            self.out.finding(
                "ROLLUP_INCOMPLETE", Severity.WARNING,
                f"{self.ref.row_names.get(row, f'row {row}')} could not be rolled up.",
                f"Components {', '.join(parts)} were not all available.", metric=str(row))

    # ------------------------------------------- 10b. category tree, any player

    def _par_row(self, par: int) -> int:
        row = self.ref.par_to_row.get(par)
        return row if row else ABSENT_ROW

    def _step_generic_tree(self) -> None:
        """Category shipped GMV, units and ASP for a player with no tuned taxonomy.

        Players store each category's share of GMV and its ASP, not its GMV. The
        reference model derives category GMV as share x platform shipped GMV, so
        the same is done here: shares are estimated from the player's own
        history, rescaled to the prior month's total so coverage stays what it
        was, then applied to the platform figure.
        """
        tree = self.cfg.get("fashion_tree")
        if self.has_categories or not tree:
            return
        shipped = self.out.num(self.P["shipped_gmv"])
        # With a MIS on the standard template the category mix is reported, not estimated.
        mis_cats = ({c.pars["share"]: c for c in self.gm.categories}
                    if self.gm is not None and self.gm.has_categories else {})
        use_mis = bool(mis_cats)
        leaves = [lf for lf in tree["leaves"]
                  if self._par_row(lf["share"]) != ABSENT_ROW or lf["share"] in mis_cats]
        if not leaves:
            self.out.finding(
                "NO_CATEGORY_SHARES", Severity.INFO,
                f"{self.cfg['display_name']}'s sheet holds no category share data.",
                "Category GMV cannot be derived, so those parameters are exported as null.")
            return

        share_pars = [lf["share"] for lf in leaves] + [
            p for p in tree.get("other_shares", [])
            if self._par_row(p) != ABSENT_ROW or p in mis_cats]
        est_share: dict[int, Value] = {}
        if use_mis:
            total = self.gm.category_total_cr
            for p in share_pars:
                c = mis_cats.get(p)
                if c is not None and c.gmv_cr:
                    est_share[p] = Value(
                        c.gmv_cr / total, Provenance.DERIVED, "%",
                        source=f"MIS {month_label(self.month)} category GMV",
                        method=f"MIS category GMV {c.gmv_cr:,.2f} Cr / MIS category total "
                               f"{total:,.2f} Cr", confidence=Confidence.HIGH)
                else:
                    est_share[p] = null("%", "not supplied in the MIS (blank or zero)")
                if self._par_row(p) != ABSENT_ROW:
                    self.out.set(self._par_row(p), est_share[p])
                    if est_share[p].is_null:
                        self._locked_null_rows.add(self._par_row(p))
            prior_sum = 1.0
        else:
            # 1. estimate every share from history, then keep the prior month's coverage
            for p in share_pars:
                est_share[p] = self._estimate_row(self._par_row(p), f"category share {p}",
                                                  "%", clip_tolerance=0.30)
                c = self.learned.get(f"par{p}", 0.0)
                v = est_share[p]
                if v.value is not None and abs(c) > 1e-9:
                    est_share[p] = Value(
                        v.value * (1 + c), v.provenance, v.unit, source=v.source,
                        method=f"{v.method}; x{1 + c:.3f} learned from earlier months' gap to "
                               "the reported and expert figures", confidence=v.confidence)
            prior_sum = sum(self._prior(self._par_row(p)) or 0.0 for p in share_pars)
            cur_sum = sum(v.value for v in est_share.values() if v.value is not None)
            k = (prior_sum / cur_sum) if cur_sum > 0 and prior_sum > 0 else 1.0
            for p, v in est_share.items():
                if v.value is None:
                    continue
                scaled = Value(v.value * k, Provenance.ESTIMATED, "%", source=v.source,
                               method=f"{v.method}; rescaled by {k:.4f} to hold the prior "
                                      f"month's coverage of {prior_sum:.1%}",
                               confidence=Confidence.LOW)
                self.out.set(self._par_row(p), scaled)
                est_share[p] = scaled

        # kept for the learning record: the shares used, and which of them were reported
        self.out.cat_shares = {p: v.value for p, v in est_share.items() if v.value is not None}
        self.out.cat_reported = {p for p in est_share if use_mis and p in mis_cats
                                 and mis_cats[p].gmv_cr}
        self._generic_leaves = leaves

        if not use_mis and prior_sum < 0.9:
            self.out.finding(
                "CATEGORY_COVERAGE_PARTIAL", Severity.INFO,
                f"This player's category data covers {prior_sum:.0%} of GMV.",
                "The category tree adds up to that share of the platform total, not to all "
                "of it, because the player's own history does not itemise the rest.")

        # 2. GMV = share x platform shipped GMV; units = GMV x 10 / ASP
        gmv: dict[int, float] = {}
        units: dict[int, float] = {}
        for lf in leaves:
            sh = est_share[lf["share"]]
            if sh.value is None or shipped is None:
                continue
            g = sh.value * shipped
            gmv[lf["gmv"]] = g
            self._put_par(lf["gmv"], Value(
                g, Provenance.DERIVED if use_mis else Provenance.ESTIMATED, "INR Cr",
                source=sh.source,
                method=("category GMV = MIS category share x platform shipped GMV" if use_mis
                        else "category GMV = estimated category share x platform shipped GMV"),
                confidence=Confidence.MEDIUM if use_mis else Confidence.LOW))
            mc = mis_cats.get(lf["share"])
            asp = None
            if mc is not None and mc.asp:
                asp = Value(mc.asp, Provenance.REPORTED, "INR",
                            source=f"MIS {month_label(self.month)} category ASP",
                            method="MIS reported category ASP", confidence=Confidence.HIGH)
            elif self._par_row(lf["asp"]) != ABSENT_ROW:
                asp = self._estimate_row(self._par_row(lf["asp"]), f"{lf['label']} ASP",
                                         "INR", clip_tolerance=0.20)
            if asp is not None:
                self._put_par(lf["asp"], asp)
                if asp.value:
                    units[lf["gmv"]] = g * 10.0 / asp.value

        # 3. rollups, resolved in dependency order. Where the player itemises only
        # some of the categories beneath a level, that level sums what exists and
        # says so, rather than going blank or implying it is complete.
        coverage: dict[int, bool] = {}
        pending = {int(k): v for k, v in tree["rollups"].items()}
        while pending:
            progressed = False
            for par, parts in list(pending.items()):
                if any(p in pending for p in parts):
                    continue
                vals = [gmv[p] for p in parts if p in gmv]
                del pending[par]
                progressed = True
                if not vals:
                    continue                  # nothing beneath it: stays null
                gmv[par] = sum(vals)
                partial = len(vals) < len(parts)
                coverage.setdefault(par, partial)
                partial = partial or any(coverage.get(p) for p in parts)
                coverage[par] = partial
                # `_put_par`, not `set_par`: a sheet that stores its own total (Snapdeal, Tata
                # CLiQ) would otherwise show a separately-estimated total that does not equal
                # the sum of its parts.
                self._put_par(par, Value(
                    gmv[par], Provenance.DERIVED, "INR Cr",
                    method=("sum of the categories this player itemises beneath it "
                            f"({len(vals)} of {len(parts)} components); it does not itemise "
                            "the rest, so this understates the full figure" if partial
                            else "sum of the categories beneath it"),
                    confidence=Confidence.LOW))
            if not progressed:
                break

        # Home, makeup and personal care, and the price buckets, straight from the MIS.
        if use_mis and shipped:
            fashion_pars = {lf["share"] for lf in leaves}
            for p, c in mis_cats.items():
                if p in fashion_pars or not c.gmv_cr or est_share.get(p) is None \
                        or est_share[p].value is None:
                    continue
                g = est_share[p].value * shipped
                self._put_par(c.pars["gmv"], Value(
                    g, Provenance.DERIVED, "INR Cr", source=est_share[p].source,
                    method="category GMV = MIS category share x platform shipped GMV",
                    confidence=Confidence.MEDIUM))
                if c.asp:
                    self._put_par(c.pars["asp"], Value(
                        c.asp, Provenance.REPORTED, "INR",
                        source=f"MIS {month_label(self.month)} category ASP",
                        method="MIS reported category ASP", confidence=Confidence.HIGH))
            bpc = [self.out.by_par[q].value for q in (250, 2776)
                   if q in self.out.by_par and self.out.by_par[q].value is not None]
            if bpc:
                self._put_par(580, Value(sum(bpc), Provenance.DERIVED, "INR Cr",
                                         method="Makeup & Fragrance + Personal Care",
                                         confidence=Confidence.MEDIUM))

            base_total = sum(self.gm.buckets.values()) + sum(u[1] for u in self.gm.unsplit)
            base = gmv.get(457) or shipped
            if base_total > 0:
                for par, cr in self.gm.buckets.items():
                    self._put_par(par, Value(
                        cr / base_total * base, Provenance.DERIVED, "INR Cr",
                        source=f"MIS {month_label(self.month)} price buckets",
                        method=f"MIS price-bucket share ({cr / base_total:.1%}) x Fashion "
                               "shipped GMV", confidence=Confidence.MEDIUM))
                for label, cr, pars in self.gm.unsplit:
                    rows = [self._par_row(q) for q in pars]
                    hist = [(self._prior(r) if r != ABSENT_ROW else None) for r in rows]
                    tot = sum(h for h in hist if h)
                    if len(pars) == 2 and all(hist) and tot > 0:
                        bucket_gmv = cr / base_total * base
                        for q, h in zip(pars, hist):
                            self._put_par(q, Value(
                                bucket_gmv * h / tot, Provenance.DERIVED, "INR Cr",
                                source=f"MIS {month_label(self.month)} price buckets",
                                method=f"MIS '{label}' bucket ({cr / base_total:.1%} of GMV) "
                                       f"split {h / tot:.0%} on the player's own "
                                       f"{month_label(self.prior)} ratio between the two rows",
                                confidence=Confidence.LOW))
                        self.out.finding(
                            "MIS_BUCKET_SPLIT", Severity.INFO,
                            f"The MIS's '{label}' bucket was split across two output rows.",
                            "The MIS total is reported; how it divides between the two rows is "
                            "taken from the player's prior-month ratio, so treat the split as an "
                            "estimate. Supply the two buckets separately to replace it.")
                    else:
                        for r in rows:
                            if r != ABSENT_ROW:
                                self._locked_null_rows.add(r)
                        self.out.finding(
                            "MIS_BUCKET_UNSPLIT", Severity.INFO,
                            f"The MIS gives one '{label}' price bucket; the output splits it "
                            f"into {len(pars)} rows.",
                            f"{cr:,.1f} Cr falls in that bucket, but the player has no prior "
                            "split to divide it by, so those rows stay null.")

        # 4. platform ASP over the categories that carry both GMV and ASP
        covered_g = sum(g for k, g in gmv.items() if k in units)
        covered_u = sum(units.values())
        need = tree.get("min_coverage_for_platform_asp", 0.6)
        cur_asp = self.out.get(self.P["asp"])
        if covered_u > 0 and shipped and covered_g / shipped >= need and \
                self.P.slot("asp") and (cur_asp is None or cur_asp.is_null):
            v = Value(covered_g * 10.0 / covered_u, Provenance.DERIVED, "INR",
                      method=f"ASP = category GMV x 10 / category units, over the categories "
                             f"with both ({covered_g / shipped:.0%} of shipped GMV)",
                      confidence=Confidence.LOW)
            self.out.set(self.P["asp"], v)
            self.out.set_par(468, v)
        if units:
            self.out.finding(
                "CATEGORY_TREE_FROM_HISTORY", Severity.INFO,
                f"Category GMV derived for {len(gmv)} categories from history.",
                "Shares and ASPs come from the player's own series; GMV is share x platform "
                "shipped GMV. All are estimates, not reported values.")

    # --------------------------------------------- 10c. everything else in the template

    def _step_category_gmv_from_shares(self) -> None:
        """Category shipped GMV = category share x shipped GMV (as the sheet does). The categories the player has a
        share for but the engine did not size (makeup, grooming, home, ...) would otherwise have no GMV at all."""
        mg = self.cfg.get("mis_generic") or {}
        tree = self.cfg.get("fashion_tree") or self.cfg.get("category_tree") or {}
        pairs = {int(lf["gmv"]): int(lf["share"]) for lf in tree.get("leaves", []) if lf.get("gmv") and lf.get("share")}
        for c in (mg.get("categories") or {}).values():
            if isinstance(c, dict) and c.get("gmv") and c.get("share"):
                pairs.setdefault(int(c["gmv"]), int(c["share"]))
        shipped = self.out.num(self.P["shipped_gmv"])
        if not shipped:
            return
        for gpar, spar in pairs.items():
            have = self.out.value_for_par(self.ref, gpar)
            if have is not None and not have.is_null and have.value is not None:
                continue
            s = self.out.value_for_par(self.ref, spar)
            if s is None or s.is_null or s.value is None:
                continue
            self._put_par(gpar, Value(
                s.value * shipped, Provenance.DERIVED if s.provenance.is_actual else s.provenance, "INR Cr",
                source=s.source, method="Category share x shipped GMV, as the sheet computes it",
                confidence=s.confidence))
        # BPC total = its categories (makeup, grooming, appliances), when the sheet gives no total of its own
        kids = [v.value for v in (self.out.value_for_par(self.ref, p) for p in (250, 2776, 4571))
                if v is not None and not v.is_null and v.value is not None]
        cur = self.out.value_for_par(self.ref, 580)
        if kids and (cur is None or cur.is_null or cur.value is None):
            self._put_par(580, Value(sum(kids), Provenance.DERIVED, "INR Cr", source="Sum of the BPC categories",
                                     method="BPC total (BPC total) = sum of its category GMVs",
                                     confidence=Confidence.MEDIUM))

    def _step_item_blocks(self) -> None:
        """Category shipped GMV, ASP and items for a sheet that carries those blocks, and the platform ASP they imply:
        items = category GMV x 10 / category ASP, ASP = shipped GMV x 10 / total items."""
        from .itemblocks import item_blocks
        blk = item_blocks(self.ref)
        shipped = self.out.num(self.P["shipped_gmv"])
        if not blk or not shipped:
            return
        ref = self.ref
        for e in blk["entries"]:
            for par, row in ((e["gpar"], e["grow"]), (e["apar"], e["arow"]), (e["ipar"], e["irow"])):
                ref.par_to_row.setdefault(par, row)
        ref.par_to_row.setdefault(blk["total"]["par"], blk["total"]["row"])
        prior = [m for m in ref.months if m < self.month]
        ship_row = self.P["shipped_gmv"]

        def have(par):
            v = self.out.value_for_par(ref, par)
            return None if v is None or v.is_null or v.value is None else v.value

        gmv: dict[int, float] = {}
        carry: dict[int, float] = {}
        for i, e in enumerate(blk["entries"]):
            g = have(e["gpar"])
            if g is not None:
                gmv[i] = g
                continue
            for m in reversed(prior):                 # its last share of shipped GMV, carried forward
                gv, sv = ref.value(e["grow"], m), ref.value(ship_row, m)
                if gv and sv:
                    carry[i] = gv / sv * shipped
                    break
        gap = shipped - sum(gmv.values())
        if carry and gap > 0 and sum(carry.values()) > 0:
            k = gap / sum(carry.values())
            for i, x in carry.items():
                gmv[i] = x * k
                e = blk["entries"][i]
                self._put_par(e["gpar"], Value(x * k, Provenance.ESTIMATED, "INR Cr",
                                               source="Last month's share of shipped GMV",
                                               method="Category GMV = last month's share of shipped GMV, carried "
                                                      "forward and scaled so the categories add to shipped GMV",
                                               confidence=Confidence.LOW))
        items: dict[int, float] = {}
        for i, e in enumerate(blk["entries"]):
            if i not in gmv:
                continue
            a = have(e["apar"])
            if a is None:
                v = self._estimate_row(e["arow"], f"{e['label']} ASP", "INR")
                if v.is_null:
                    continue
                self._put_par(e["apar"], v)
                a = v.value
            if a:
                items[i] = gmv[i] * 10.0 / a
                self._put_par(e["ipar"], Value(items[i], Provenance.DERIVED, "Mn",
                                               source="Category shipped GMV and ASP",
                                               method="Items = category shipped GMV x 10 / category ASP",
                                               confidence=Confidence.MEDIUM))
        if not items:
            return
        total = sum(items.values())
        self._put_par(blk["total"]["par"], Value(total, Provenance.DERIVED, "Mn", source="Sum of the categories",
                                                 method="Total items = sum of the category items",
                                                 confidence=Confidence.MEDIUM))
        cur = self.out.get(self.P["asp"])
        asp = shipped * 10.0 / total
        if cur is not None and not cur.is_null and cur.provenance != Provenance.REPORTED and cur.value \
                and abs(asp / cur.value - 1) <= 0.25:
            self.out.set(self.P["asp"], Value(asp, Provenance.DERIVED, "INR", source="Category GMV and ASP",
                                              method="ASP = shipped GMV x 10 / total items",
                                              confidence=Confidence.MEDIUM))
        bk = ref.par_to_row.get(2770)
        aov, asp_now = self.out.num(self.P["aov"]), self.out.num(self.P["asp"])
        if bk and have(2770) is None and aov and asp_now:
            self._put_par(2770, Value(aov / asp_now, Provenance.DERIVED, "#", method="Basket size = AOV / ASP",
                                      confidence=Confidence.MEDIUM))

    def _step_price_buckets(self) -> None:
        """Price buckets are shares of shipped GMV (in the master a bucket is its share x shipped GMV), so
        together they must equal it. Each bucket is estimated from its own history, so their total drifts;
        scale them back onto shipped GMV, keeping the mix across buckets."""
        import re
        mg = self.cfg.get("mis_generic") or {}
        pars = {int(p) for p in (mg.get("price_buckets") or {}).values()}
        for v in (mg.get("unsplit_buckets") or {}).values():
            pars.update(int(p) for p in v)
        pars |= {t.par_id for t in self.ref.template if re.search(r"(?i)price bucket", t.parent_question or "")}
        shipped = self.out.num(self.P["shipped_gmv"])
        live = {}
        for p in pars:
            v = self.out.value_for_par(self.ref, p)
            if v is not None and not v.is_null and v.value is not None:
                live[p] = v
        if len(live) < 2 or not shipped:
            return
        total = sum(v.value for v in live.values())
        if total <= 0 or abs(total / shipped - 1) < 1e-9:
            return
        k = shipped / total
        for p, v in live.items():
            self._put_par(p, Value(
                v.value * k, Provenance.DERIVED if v.provenance.is_actual else v.provenance, v.unit,
                source=v.source,
                method=f"{v.method}; rescaled by {k:.4f} so the price buckets add up to shipped GMV "
                       "(a bucket is its share x shipped GMV)", confidence=v.confidence))
        self.out.finding(
            "PRICE_BUCKETS_RESCALED", Severity.INFO,
            f"Price buckets rescaled by {k:.3f} to add up to shipped GMV.",
            f"They summed to {total:,.2f} Cr against shipped GMV of {shipped:,.2f} Cr; the mix across "
            "the buckets is unchanged.")

    def _step_template_fill(self) -> None:
        """Estimate any template parameter the player's sheet carries and nothing has produced.

        This is what lets a run with no MIS and no call still deliver the whole
        sheet: every metric the player's own history supports gets a value, each
        marked as an estimate with its method. Metrics the sheet does not carry
        stay null.
        """
        # A row holding an explicit null was given up on by an earlier step that
        # needed other inputs; its own history may still support an estimate.
        done_rows = {r for r, v in self.out.rows.items() if v is not None and not v.is_null}
        groups = [g for g in (self.cfg.get("split_groups") or {}).get("groups", [])]
        in_group = {p for g in groups for p in g}
        filled = 0

        for t in self.ref.template:
            row = self.ref.par_to_row.get(t.par_id)
            if not row or row in done_rows or t.par_id in in_group \
                    or row in self._locked_null_rows:
                continue
            v = self._estimate_row(row, t.parameter_name, t.unit)
            if v.is_null:
                # Keep the reason (e.g. "history ends Sep'24, too stale to project") so
                # the Source sheet explains each null rather than leaving it blank.
                self.out.set(row, v)
                continue
            self.out.set(row, v)
            filled += 1

        for g in groups:
            rows = [self._par_row(p) for p in g]
            if any(r == ABSENT_ROW for r in rows) or any(r in done_rows for r in rows):
                continue
            vals = [self._estimate_row(r, f"split member {p}", "%", clip_tolerance=0.30)
                    for r, p in zip(rows, g)]
            if any(v.is_null for v in vals):
                continue
            prior_sum = sum(self._prior(r) or 0.0 for r in rows)
            cur = sum(v.value for v in vals)
            if cur <= 0 or prior_sum <= 0:
                continue
            k = prior_sum / cur
            for r, v in zip(rows, vals):
                self.out.set(r, Value(v.value * k, Provenance.ESTIMATED, v.unit,
                                      source=v.source,
                                      method=f"{v.method}; the split is rescaled together "
                                             "to its prior-month total",
                                      confidence=Confidence.LOW))
                filled += 1
        if filled:
            self.out.finding(
                "TEMPLATE_FILLED_FROM_HISTORY", Severity.INFO,
                f"{filled} further parameters estimated from the player's own history.",
                "No MIS or call figure existed for these, so each is a history-based estimate "
                "(trailing level x seasonal factor) and is marked as such.")

    # --------------------------------------------------------- 11. identities

    def _step_own_model(self) -> None:
        """Tata CLiQ, Snapdeal: apply the sheet's own formulas to this month, so the categories add up
        (parent = parts, items = GMV / ASP, shares of one parent sum to 1, category GMV = platform GMV ...).
        Run twice: before the item blocks, and again once their ASP and items exist."""
        from ..export.webform import OWN_LAYOUT, slug
        if slug(self.cfg.get("display_name") or "") not in OWN_LAYOUT:
            return
        from . import ownmodel
        ownmodel.apply(self)
        row = self.ref.par_to_row.get(2770)                       # basket size = AOV / ASP, exactly
        aov, asp = self.out.num(self.P["aov"]), self.out.num(self.P["asp"])
        cur = self.out.get(row) if row else None
        if row and aov and asp and not (cur is not None and cur.provenance.is_actual):
            self.out.set(row, Value(aov / asp, Provenance.DERIVED, "#", source="AOV and ASP",
                                    method="Basket size = AOV / ASP", confidence=Confidence.MEDIUM))

    def _step_sheet_rollups(self) -> None:
        """Make a category total equal the sum of its parts where the sheet stores both.

        Snapdeal and Tata CLiQ keep each category total as its own series, so each is estimated
        on its own and the parts need not add up to the whole. Where every part exists the whole
        is set to their sum, as it is for the other players. A reported figure is never replaced.
        """
        tree = self.cfg.get("fashion_tree")
        if not tree:
            return
        pending = {int(k): list(v) for k, v in tree["rollups"].items()}
        while pending:
            progressed = False
            for par, parts in list(pending.items()):
                if any(p in pending for p in parts):
                    continue
                del pending[par]
                progressed = True
                vals = [self.out.value_for_par(self.ref, p) for p in parts]
                if any(v is None or v.is_null or v.value is None for v in vals):
                    continue
                cur = self.out.value_for_par(self.ref, par)
                if cur is not None and not cur.is_null and cur.provenance.is_actual:
                    continue
                total = sum(v.value for v in vals)
                if cur is not None and cur.value is not None and abs(cur.value - total) <= 1e-9:
                    continue
                self._put_par(par, Value(
                    total, Provenance.DERIVED, "INR Cr",
                    method="sum of the categories beneath it; the player's sheet stores each "
                           "category itself, so the total is set to match them",
                    confidence=Confidence.LOW))
            if not progressed:
                break

    def _step_identities(self) -> None:
        P = self.P
        checks: list[tuple[str, float | None, float | None, float, str]] = []

        shipped, canc, gross = (self.out.num(P["shipped_gmv"]),
                                self.out.num(P["cancellation_pct"]),
                                self.out.num(P["gross_gmv"]))
        if None not in (shipped, canc, gross):
            checks.append(("gross_from_shipped", gross * (1 - canc), shipped, 1e-6,
                           "Gross GMV x (1 - cancellation) must equal Shipped GMV"))

        net, ret = self.out.num(P["net_gmv"]), self.out.num(P["returns_pct"])
        if None not in (net, shipped, ret):
            checks.append(("net_from_shipped", net, shipped * (1 - ret), 1e-6,
                           "Net GMV must equal Shipped GMV x (1 - returns)"))

        rto, rvp = self.out.num(P["rto_pct"]), self.out.num(P["rvp_pct"])
        if None not in (rto, rvp, ret):
            checks.append(("returns_split", rto + rvp, ret, 1e-9,
                           "RTO + RVP must equal the returns rate"))

        seg_sum = sum(v for s in self.S
                      if (v := self.out.num(self.S[s]["shipped_gmv"])) is not None)
        if shipped is not None and seg_sum:
            checks.append(("segment_total", seg_sum, shipped, 1e-6,
                           "Segment shipped GMV must sum to the platform total"))

        l2_sum = sum(v["gmv"].value for v in self.out.l2.values()
                     if v.get("gmv") and v["gmv"].value is not None)
        if shipped is not None and l2_sum:
            checks.append(("l2_total", l2_sum, shipped, 1e-6,
                           "L2 category GMV must sum to the platform total"))

        cod, pre = self.out.num(P["cod_pct"]), self.out.num(P["prepaid_pct"])
        if None not in (cod, pre):
            checks.append(("payment_split", cod + pre, 1.0, 1e-9, "COD + Prepaid must equal 1"))

        geo_sum = sum(v for r in (P["metro_pct"], P["tier1_pct"], P["tier2plus_pct"])
                      if (v := self.out.num(r)) is not None)
        if geo_sum:
            checks.append(("geo_split", geo_sum, 1.0, 1e-9,
                           "Metro + Tier 1 + Tier 2+ must equal 1"))

        share_sum = sum(v for k in self.L2 if (v := self.out.num(self.L2[k]["share"])) is not None)
        if share_sum:
            checks.append(("contrib_shares", share_sum, 1.0, 1e-6,
                           "L2 contribution shares must sum to 1"))

        for name, lhs, rhs, tol, desc in checks:
            if lhs is None or rhs is None:
                continue
            scale = max(abs(rhs), 1e-9)
            if abs(lhs - rhs) / scale > tol:
                self.out.finding(
                    f"IDENTITY_{name.upper()}", Severity.ERROR,
                    f"Reconciliation failed: {desc}.",
                    f"{lhs:,.6f} vs {rhs:,.6f} (relative gap {abs(lhs - rhs) / scale:.2e}).",
                    metric=name)
            else:
                self.out.finding(f"IDENTITY_{name.upper()}", Severity.INFO,
                                 f"Reconciled: {desc}.", f"{lhs:,.4f} = {rhs:,.4f}", metric=name)

        # A known inconsistency in the reference model, reported rather than forced.
        plat_txn = self.out.num(P["gross_transactions"])
        seg_txn = sum(v for s in self.S
                      if (v := self.out.num(self.S[s]["transactions"])) is not None)
        if plat_txn and seg_txn:
            gap = seg_txn / plat_txn - 1
            if abs(gap) > 0.02:
                self.out.finding(
                    "MODEL_TXN_MISMATCH", Severity.WARNING,
                    f"Segment transactions sum to {seg_txn:,.2f} Mn against a platform figure of "
                    f"{plat_txn:,.2f} Mn ({gap:+.1%}).",
                    "The reference workbook derives these two on different AOV bases and does not "
                    "reconcile them; the same gap is present in its own historical columns. "
                    "Carried forward unchanged rather than silently forced.",
                    metric="gross_transactions")

    # ------------------------------------------------- 12. call corroboration

    def _balance_gross(self, v: Value) -> Value:
        """Keep MoM and YoY from being far off each other, using the stored history.

        The MIS states a level and its MoM and YoY. On the stored history the level gives
        different ones. The level whose growth, on that history, best matches both is found
        and the reported level is moved toward it by the history weight, by no more than the
        cap. The reported figure is kept on the Value (`uncalibrated`) and in the audit trail.
        """
        hb = self.cfg.get("history_balance") or {}
        ov = self.growth_override
        if (self.gm is None and not ov) or not hb.get("enabled", True) or "gross_gmv" not in hb.get(
                "applies_to", ["gross_gmv"]):
            return v
        mom, yoy = self.gm.growth.get("gross_gmv", (None, None)) if self.gm else (None, None)
        if ov.get("mom") is not None:
            mom = ov["mom"]
        if ov.get("yoy") is not None:
            yoy = ov["yoy"]
        row = self.P["gross_gmv"]
        prior, year = self.ref.value(row, self.prior), self.ref.value(row, self.yoy_month)
        import datetime as _dt
        y, m = int(self.yoy_month[:4]), int(self.yoy_month[5:7])
        call_base = (self.tr.stated_values.get(f"{_dt.date(y, m, 1):%b} {y} GMV")
                     if self.tr else None)
        r = reconcile_growth(
            "Gross GMV", v.value, v.provenance.value, prior, year, mom_stated=mom,
            yoy_stated=yoy, call_year_ago=call_base,
            level_is_reported=v.provenance is Provenance.REPORTED,
            label_prior=month_label(self.prior), label_year=month_label(self.yoy_month))
        self.out.reconciliations[241] = r
        target = balanced_level(prior, year, mom, yoy)
        if target is None or r.diagnosis in ("consistent", "insufficient"):
            r.final_level = v.value
            return v
        base_w = self.history_weight if self.history_weight is not None else hb.get("weight", 1.0)
        gaps = [c["history_gap"] for c in self.past_calibration if c.get("history_gap") is not None]
        w, basis = trusted_weight(base_w, gaps, hb.get("max_gap_for_trust", 0.20),
                                  hb.get("min_weight", 0.25)) if base_w > 0 else (0.0, "history weight is 0")
        new, capped = blend(v.value, target, w, hb.get("max_adjustment", 0.15))
        r.balanced_level, r.blend_weight, r.weight_basis, r.capped = target, w, basis, capped
        r.final_level = new
        r.mom_final = (new / prior - 1) if prior else None
        r.yoy_final = (new / year - 1) if year else None
        if abs(new / v.value - 1) < 1e-9:
            return v
        why = (f"The MIS reports {v.value:,.1f} Cr with MoM {mom:+.1%} and YoY {yoy:+.1%}. On the "
               f"stored history that level gives MoM {r.mom_stored:+.1%} and YoY {r.yoy_stored:+.1%}. "
               f"The level whose growth on that history best matches both is {target:,.1f}; the "
               f"output moves {w:.0%} of the way there to {new:,.1f}"
               + (" (capped)" if capped else "") + f", giving MoM {r.mom_final:+.1%} and YoY "
               f"{r.yoy_final:+.1%}. History weight: {basis}.")
        self.out.adjustments.append(Adjustment(
            metric="Gross GMV", driver="history balance", before=v.value, after=new,
            rationale=why, source="stored history and the MIS's stated MoM and YoY"))
        self.out.finding(
            "HISTORY_BALANCE", Severity.INFO,
            f"Gross GMV is {new:,.1f} Cr, {new / v.value - 1:+.1%} against the MIS's "
            f"{v.value:,.1f} Cr, to keep MoM and YoY consistent with the stored history.",
            why + " The MIS figure is kept beside the output; set the history weight to 0 to "
                  "use it unchanged.", metric="Gross GMV")
        return Value(new, Provenance.CALIBRATED, "INR Cr", source=v.source,
                     method=f"MIS Gross GMV {v.value:,.1f} balanced against stored history "
                            f"({w:.0%} weight): YoY {r.yoy_final:+.1%}, MoM {r.mom_final:+.1%}",
                     confidence=Confidence.MEDIUM, uncalibrated=v.value)

    def _step_growth_reconciliation(self) -> None:
        """Test the level against the MoM and YoY the MIS states, and say which input is off."""
        if self.gm is None:
            return
        import datetime as _dt
        y, m = int(self.yoy_month[:4]), int(self.yoy_month[5:7])
        call_base = (self.tr.stated_values.get(f"{_dt.date(y, m, 1):%b} {y} GMV")
                     if self.tr else None)
        for key, par, label, is_gmv in (("gross_gmv", 241, "Gross GMV", True),
                                        ("aov", 240, "AOV", False)):
            v = self.out.get(self.P[key])
            mom, yoy = self.gm.growth.get(key, (None, None))
            if v is None or v.is_null or (mom is None and yoy is None) \
                    or par in self.out.reconciliations:        # gross GMV was done up front
                continue
            row = self.P[key]
            r = reconcile_growth(
                label, v.value, v.provenance.value,
                self.ref.value(row, self.prior), self.ref.value(row, self.yoy_month),
                mom_stated=mom, yoy_stated=yoy,
                call_year_ago=call_base if is_gmv else None,
                level_is_reported=v.provenance is Provenance.REPORTED,
                label_prior=month_label(self.prior), label_year=month_label(self.yoy_month))
            self.out.reconciliations[par] = r
            if r.adjusted_level and v.provenance is not Provenance.REPORTED:
                self.out.adjustments.append(Adjustment(
                    metric=label, driver="growth reconciliation", before=v.value,
                    after=r.adjusted_level, rationale=r.narrative, source="MIS growth rates"))
                self.out.set(row, Value(r.adjusted_level, Provenance.CALIBRATED, v.unit,
                                        source=v.source,
                                        method=v.method + "; placed between the level and the "
                                               "growth-implied levels",
                                        confidence=Confidence.LOW, uncalibrated=v.value))
            if r.diagnosis == "consistent":
                continue
            self.out.finding(
                "GROWTH_RECONCILIATION",
                Severity.WARNING if r.diagnosis == "level" else Severity.INFO,
                f"{label}: stated growth and reported level {'agree' if r.diagnosis == 'consistent' else 'disagree'}"
                f" on the stored history; the {r.diagnosis.replace('_', ' ')} is the odd one out.",
                r.narrative, metric=label)

    def _step_history_base_check(self) -> None:
        """Set the stored year-ago GMV against the year-ago figure the expert gives.

        The seasonal pattern for a month is learned from stored history. If the stored
        year-ago month is inflated, every history-only estimate for this calendar month
        inherits an inflated uplift. The stored value is not changed; the gap is reported.
        """
        if not self.tr or not self.P.has("gross_gmv"):
            return
        import datetime as _dt
        y, m = int(self.yoy_month[:4]), int(self.yoy_month[5:7])
        stated = self.tr.stated_values.get(f"{_dt.date(y, m, 1):%b} {y} GMV")
        stored = self.ref.value(self.P["gross_gmv"], self.yoy_month)
        if not stated or not stored:
            return
        gap = stored / stated - 1
        self.out.finding(
            "HISTORY_BASE_CONFLICT",
            Severity.WARNING if abs(gap) > 0.05 else Severity.INFO,
            f"Stored {month_label(self.yoy_month)} gross GMV is {stored:,.1f} Cr; the call puts "
            f"it near {stated:,.0f} Cr ({gap:+.0%}).",
            "The seasonal uplift for this calendar month is learned from stored history, so an "
            "overstated year-ago month overstates the uplift in any history-only estimate for "
            "it. The stored value was not changed; treat history-only figures for this month "
            "with extra caution.", metric="gross_gmv")

    def _step_call_corroboration(self) -> None:
        """Compare the call's category YoY with what the MIS actually implies.

        The call is not permitted to move the MIS anchor, so this produces
        findings and feeds the calibration table rather than changing numbers.
        """
        if not self.tr or not self.tr.category_yoy:
            return
        if not self.tr.platform_gmv_given:
            claimed = self.out.claim_driver(
                "festive event timing shift",
                "expert call — YoY weakness attributed to the festive calendar moving later")
            if not claimed:
                self.out.notes.append(
                    "The expert attributes this month's YoY weakness to event timing, which the "
                    "calendar lever has already accounted for by suppressing the seasonal "
                    "factor. No second adjustment is applied for the same driver.")

        aliases = self.cfg.get("call_category_aliases", {})
        tmap = self.cfg.get("mis_taxonomy_map") or {}
        secs = self.cfg.get("mis_sections", {})
        if not tmap or not self.has_categories:
            return
        mis_gmv = ({k: v / self.mis_scale for k, v in self.mis.section_values(
                    self.month, secs.get("category_gmv", "Category GMV")).items()}
                   if self.mis and self.month in self.mis.months else {})

        agree = disagree = 0
        for call_cat, yoy_call in self.tr.category_yoy.items():
            mis_cat = aliases.get(call_cat, call_cat)
            if mis_cat not in mis_gmv or mis_cat not in tmap:
                continue
            targets = tmap[mis_cat]["targets"]
            hist = sum(self.ref.value(self.L2[t]["gmv"], self.yoy_month) or 0.0 for t in targets)
            if hist <= 0:
                continue
            implied_yoy = mis_gmv[mis_cat] / hist - 1
            gap = implied_yoy - yoy_call
            if abs(gap) <= 0.10:
                agree += 1
            else:
                disagree += 1
                self.out.finding(
                    "CALL_MIS_HISTORY_CONFLICT", Severity.WARNING,
                    f"{mis_cat}: the call says {yoy_call:+.0%} YoY but the MIS against the stored "
                    f"{month_label(self.yoy_month)} value implies {implied_yoy:+.0%}.",
                    f"MIS {month_label(self.month)} = {mis_gmv[mis_cat]:,.1f} Cr; stored "
                    f"{month_label(self.yoy_month)} = {hist:,.1f} Cr. The MIS actual and the "
                    f"call YoY are mutually consistent only if "
                    f"{month_label(self.yoy_month)} was really "
                    f"{mis_gmv[mis_cat] / (1 + yoy_call):,.1f} Cr, so the stored history is the "
                    "most likely source of the gap. Quantified in the calibration sheet.",
                    metric=mis_cat)

        if agree or disagree:
            self.out.finding(
                "CALL_CORROBORATION", Severity.INFO,
                f"Category corroboration: {agree} of {agree + disagree} categories agree within "
                "10pp between the call YoY and the MIS-vs-stored-history YoY.",
                "Disagreement here measures error in the stored history, not in the MIS, "
                "because the MIS is a reported actual for the current month.")
