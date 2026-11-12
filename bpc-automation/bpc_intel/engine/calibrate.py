"""Historical calibration and honest accuracy measurement.

Two separate things live here and are deliberately kept apart:

* **Measured accuracy** — backtests of the estimators against the model's own
  history, walked forward so no future information reaches an estimate. This is
  real, out-of-sample error.
* **Implied historical error** — what the current month's reported actual plus
  the expert's stated YoY says the year-ago stored value must have been. This
  is evidence about the *history*, not about forecast accuracy, and it is
  reported as such.

Nothing here rewrites a stored value. The bias terms produced are offered for
future months and are never applied to a reported actual.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from ..domain import month_label, shift_months
from ..readers.reference import ReferenceModel
from ..readers.transcript import TranscriptData
from . import estimators as est


@dataclass
class ImpliedError:
    category: str
    targets: list[str]
    mis_value: float
    call_yoy: float
    implied_prior: float
    stored_prior: float

    @property
    def error_pct(self) -> float:
        """How far the stored history sits from what current evidence implies."""
        return self.stored_prior / self.implied_prior - 1.0

    @property
    def direction(self) -> str:
        return "overstated" if self.error_pct > 0 else "understated"

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "model_targets": ", ".join(self.targets),
            "mis_current": self.mis_value,
            "call_yoy": self.call_yoy,
            "implied_prior": self.implied_prior,
            "stored_prior": self.stored_prior,
            "error_pct": self.error_pct,
            "direction": self.direction,
        }


@dataclass
class CalibrationReport:
    month: str
    prior_month: str
    implied: list[ImpliedError] = field(default_factory=list)
    backtests: list[est.BacktestResult] = field(default_factory=list)
    bias_terms: dict[str, float] = field(default_factory=dict)
    statements: list[str] = field(default_factory=list)
    accuracy_measured: bool = False

    # ---- aggregate statistics over the implied-error table ----

    @property
    def n(self) -> int:
        return len(self.implied)

    @property
    def mape(self) -> float | None:
        return statistics.fmean(abs(i.error_pct) for i in self.implied) if self.implied else None

    @property
    def mpe(self) -> float | None:
        """Signed mean: positive means stored history runs above implied actuals."""
        return statistics.fmean(i.error_pct for i in self.implied) if self.implied else None

    @property
    def systematic(self) -> str:
        m = self.mpe
        if m is None:
            return "not measurable"
        if abs(m) < 0.05:
            return "no material systematic bias"
        return "systematic overstatement" if m > 0 else "systematic understatement"

    def to_dict(self) -> dict[str, Any]:
        return {
            "month": self.month,
            "prior_month": self.prior_month,
            "n_categories": self.n,
            "mape": self.mape,
            "mpe": self.mpe,
            "systematic": self.systematic,
            "accuracy_measured": self.accuracy_measured,
            "implied": [i.to_dict() for i in self.implied],
            "backtests": [b.to_dict() for b in self.backtests],
            "bias_terms": dict(self.bias_terms),
            "statements": list(self.statements),
        }


def calibrate(
    cfg: dict,
    ref: ReferenceModel,
    month: str,
    mis_category_gmv: dict[str, float] | None,
    transcript: TranscriptData | None,
    backtest_metrics: dict[str, int] | None = None,
) -> CalibrationReport:
    prior_year = shift_months(month, -12)
    rep = CalibrationReport(month=month, prior_month=prior_year)
    L2 = cfg.get("l2_categories") or {}
    tmap = cfg.get("mis_taxonomy_map") or {}
    aliases = cfg.get("call_category_aliases", {})

    # ---- implied historical error -----------------------------------------
    if transcript and transcript.category_yoy and mis_category_gmv:
        for call_cat, yoy in transcript.category_yoy.items():
            mis_cat = aliases.get(call_cat, call_cat)
            if mis_cat not in mis_category_gmv or mis_cat not in tmap:
                continue
            if yoy <= -1:
                continue
            targets = tmap[mis_cat]["targets"]
            stored = sum(ref.value(L2[t]["gmv"], prior_year) or 0.0 for t in targets)
            if stored <= 0:
                continue
            cur = mis_category_gmv[mis_cat]
            implied = cur / (1 + yoy)
            rep.implied.append(ImpliedError(
                category=mis_cat, targets=targets, mis_value=cur, call_yoy=yoy,
                implied_prior=implied, stored_prior=stored,
            ))

    if rep.implied:
        rep.statements.append(
            f"{rep.n} categories have both a reported {month_label(month)} actual and an expert "
            f"YoY, so {month_label(prior_year)} can be back-implied for each. Mean absolute "
            f"deviation of the stored history from those implied values is {rep.mape:.1%}; the "
            f"signed mean is {rep.mpe:+.1%} ({rep.systematic})."
        )
        rep.statements.append(
            "This measures error in the stored historical series, not forecast accuracy. The "
            "stored values were not changed."
        )
        for i in rep.implied:
            # Offered for future months only, never applied to a reported actual.
            rep.bias_terms[i.category] = i.error_pct
    else:
        rep.statements.append(
            "No category had both a reported actual and an expert YoY, so no historical error "
            "could be measured this run."
        )

    # ---- genuine out-of-sample backtests ----------------------------------
    for label, row in (backtest_metrics or {}).items():
        series = {m: v for m, v in ref.series.get(row, {}).items() if m <= month}
        r = est.backtest(series, label)
        if r.n:
            rep.backtests.append(r)

    rep.accuracy_measured = any(b.n > 0 for b in rep.backtests)
    if rep.accuracy_measured:
        for b in rep.backtests:
            rep.statements.append(
                f"Backtest '{b.metric}': {b.n} walk-forward months, MAPE {b.mape:.1%}, "
                f"signed mean error {b.mpe:+.1%}, direction correct "
                f"{b.direction_accuracy:.0%}." if b.direction_accuracy is not None else
                f"Backtest '{b.metric}': {b.n} months, MAPE {b.mape:.1%}."
            )
    else:
        rep.statements.append(
            "No estimator had enough history for a walk-forward backtest, so no forecast "
            "accuracy is claimed for this run."
        )

    rep.statements.append(
        "Confirmed actuals for this month's estimated parameters are not yet available, so no "
        "improvement in forecast accuracy is asserted. Accuracy can only be measured once "
        "subsequent actuals arrive."
    )
    return rep


def apply_bias(value: float, metric: str, report: CalibrationReport,
               cap: float = 0.25) -> tuple[float, str]:
    """Apply a measured bias term to a *model-estimated* value.

    Capped, and only ever used where there is no reported actual. Returns the
    value unchanged when no term was measured for that metric.
    """
    term = report.bias_terms.get(metric)
    if term is None:
        return value, "no measured bias term for this metric; value unchanged"
    adj = max(-cap, min(cap, term))
    corrected = value / (1 + adj)
    return corrected, (
        f"bias correction {adj:+.1%} from the measured historical deviation for {metric} "
        f"(capped at +/-{cap:.0%})"
    )
