"""Point estimators for parameters that have no direct evidence this month.

Deliberately simple and interpretable, because they have to be backtested
honestly and explained in a footnote. The only estimator used for levels is a
seasonal-naive one: a recent level times a same-calendar-month factor measured
on earlier years.

Every function here takes history that ends strictly before the target month.
Nothing may look at the target month's own value.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Sequence

from ..domain import Confidence, shift_months


@dataclass
class Estimate:
    value: float | None
    method: str
    confidence: Confidence
    basis: str = ""
    n_obs: int = 0
    seasonal_factor: float | None = None
    clipped: bool = False

    @property
    def ok(self) -> bool:
        return self.value is not None


def _lookup(series: dict[str, float], month: str) -> float | None:
    v = series.get(month)
    return float(v) if isinstance(v, (int, float)) else None


def trailing_mean(series: dict[str, float], before: str, n: int = 3) -> tuple[float | None, int]:
    """Mean of the n most recent observations strictly before `before`."""
    vals = [v for m, v in sorted(series.items()) if m < before and v is not None]
    if not vals:
        return None, 0
    take = vals[-n:]
    return statistics.fmean(take), len(take)


def seasonal_factor(
    series: dict[str, float], target: str, n_years: int = 3, window: int = 3
) -> tuple[float | None, int]:
    """Average of (same calendar month) / (trailing mean before it) over prior years.

    A factor of 1.18 means this calendar month historically runs 18% above the
    three months preceding it.
    """
    y, m = (int(x) for x in target.split("-"))
    ratios: list[float] = []
    for back in range(1, n_years + 1):
        key = f"{y - back:04d}-{m:02d}"
        v = _lookup(series, key)
        if v is None:
            continue
        base, n = trailing_mean(series, key, window)
        if base in (None, 0) or n < window:
            continue
        ratios.append(v / base)
    if not ratios:
        return None, 0
    return statistics.fmean(ratios), len(ratios)


def _shift(month: str, k: int) -> str:
    y, m = int(month[:4]), int(month[5:7])
    t = y * 12 + (m - 1) + k
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def _trend_candidates(series: dict[str, float], target: str, n_years: int = 3, strength: float = 1.0) -> list[float]:
    """[last year's month x the recent YoY, last month x the usual seasonal month-on-month], where each can be built."""
    out: list[float] = []
    ly = _lookup(series, _shift(target, -12))
    yoys = []
    for k in (1, 2, 3):
        a, b = _lookup(series, _shift(target, -k)), _lookup(series, _shift(target, -k - 12))
        if a and b and b > 0:
            yoys.append(a / b)
    if ly and len(yoys) >= 2:
        out.append(ly * statistics.fmean(yoys))
    prior = _lookup(series, _shift(target, -1))
    moms = []
    for back in range(1, n_years + 1):
        a, b = _lookup(series, _shift(target, -12 * back)), _lookup(series, _shift(target, -12 * back - 1))
        if a and b and b > 0:
            moms.append(a / b)
    if prior and moms:
        out.append(prior * (1.0 + strength * (statistics.fmean(moms) - 1.0)))
    return out


def seasonal_naive(
    series: dict[str, float],
    target: str,
    window: int = 3,
    n_years: int = 3,
    clip_to_history: bool = True,
    clip_months: int = 12,
    clip_tolerance: float = 0.15,
    apply_seasonal: bool = True,
    seasonal_suppression_reason: str = "",
    seasonal_strength: float = 1.0,
) -> Estimate:
    """Trailing level scaled by the month's historical seasonal factor.

    `apply_seasonal=False` holds the estimate at the trailing level. That matters
    when the recurring event behind a calendar month's seasonality is known not
    to have happened this year — applying the factor would otherwise import an
    event that did not occur.
    """
    level, n = trailing_mean(series, target, window)
    if level is None:
        return Estimate(None, "seasonal_naive", Confidence.NONE,
                        basis="no history before the reporting month", n_obs=0)

    sf, n_years_used = seasonal_factor(series, target, n_years, window)
    if not apply_seasonal:
        est = level
        method = (f"trailing {n}-month mean, seasonal factor "
                  + (f"{sf:.3f} " if sf is not None else "")
                  + f"suppressed: {seasonal_suppression_reason}")
        conf = Confidence.MEDIUM
    elif sf is None:
        est, method, conf = level, f"trailing {n}-month mean (no seasonal history)", Confidence.LOW
    else:
        # A strength below 1 keeps only part of the seasonal swing: the same factor, damped.
        eff = 1.0 + seasonal_strength * (sf - 1.0)
        est = level * eff
        # The trailing mean lags the latest month, so a spike or dip there makes the month-on-month wild. Two trend
        # readings are set beside it, and the middle of the three is used: last year's month x the recent YoY, and last
        # month x the usual seasonal month-on-month.
        tr = _trend_candidates(series, target, n_years, strength=seasonal_strength)
        if tr:
            cands = sorted([est] + tr)
            est = cands[1] if len(cands) == 3 else statistics.fmean(cands)
        method = (f"trailing {n}-month mean x seasonal factor {sf:.3f} from {n_years_used} "
                  f"prior year(s)" + (", checked against the YoY and month-on-month trend" if tr else "")
                  + (f", damped to {eff:.3f} (strength {seasonal_strength:.2f}, chosen by "
                     "walk-forward backtest)" if seasonal_strength != 1.0 else ""))
        conf = Confidence.MEDIUM if n_years_used >= 2 else Confidence.LOW

    clipped = False
    if clip_to_history:
        recent = [v for m, v in sorted(series.items()) if m < target][-clip_months:]
        if recent:
            lo = min(recent) * (1 - clip_tolerance)
            hi = max(recent) * (1 + clip_tolerance)
            if est < lo:
                est, clipped = lo, True
            elif est > hi:
                est, clipped = hi, True

    return Estimate(
        value=est, method=method, confidence=conf,
        basis=f"{n} trailing obs, {n_years_used} seasonal year(s)"
              + ("" if apply_seasonal else ", seasonal factor suppressed"),
        n_obs=n, seasonal_factor=sf, clipped=clipped,
    )


def ratio_split(total: float, parts: dict[str, float]) -> dict[str, float]:
    """Allocate `total` across parts in proportion to their historical values."""
    s = sum(v for v in parts.values() if v is not None)
    if s <= 0:
        raise ValueError("Cannot split a total across parts with no positive historical basis.")
    return {k: total * (v / s) for k, v in parts.items() if v is not None}


def yoy(series: dict[str, float], month: str) -> float | None:
    cur, prev = _lookup(series, month), _lookup(series, shift_months(month, -12))
    if cur is None or prev in (None, 0):
        return None
    return cur / prev - 1.0


def mom(series: dict[str, float], month: str) -> float | None:
    cur, prev = _lookup(series, month), _lookup(series, shift_months(month, -1))
    if cur is None or prev in (None, 0):
        return None
    return cur / prev - 1.0


# ----------------------------------------------------------------- backtesting


@dataclass
class BacktestResult:
    metric: str
    n: int
    mape: float | None
    mpe: float | None            # signed: positive means the estimator runs high
    direction_accuracy: float | None
    per_month: list[dict]

    def to_dict(self) -> dict:
        return {
            "metric": self.metric, "n": self.n,
            "mape": self.mape, "mpe": self.mpe,
            "direction_accuracy": self.direction_accuracy,
            "per_month": self.per_month,
        }


def backtest(
    series: dict[str, float],
    metric: str,
    min_history: int = 15,
    estimator=seasonal_naive,
    **kwargs,
) -> BacktestResult:
    """Walk forward through history, re-estimating each month from prior data only.

    At month *t* the estimator is handed a series truncated to *< t*, so no
    information from *t* or later can leak into its own estimate.
    """
    months = sorted(m for m, v in series.items() if v is not None)
    rows: list[dict] = []
    for i, m in enumerate(months):
        if i < min_history:
            continue
        past = {k: series[k] for k in months[:i]}
        est = estimator(past, m, **kwargs)
        actual = series[m]
        if not est.ok or actual in (None, 0):
            continue
        err = est.value - actual
        prev = series.get(months[i - 1])
        dir_ok = None
        if prev not in (None, 0):
            dir_ok = (est.value >= prev) == (actual >= prev)
        rows.append({
            "month": m, "actual": actual, "estimate": est.value,
            "error": err, "abs_pct_error": abs(err / actual),
            "pct_error": err / actual, "direction_correct": dir_ok,
            "method": est.method,
        })

    if not rows:
        return BacktestResult(metric, 0, None, None, None, [])
    dirs = [r["direction_correct"] for r in rows if r["direction_correct"] is not None]
    return BacktestResult(
        metric=metric,
        n=len(rows),
        mape=statistics.fmean(r["abs_pct_error"] for r in rows),
        mpe=statistics.fmean(r["pct_error"] for r in rows),
        direction_accuracy=(sum(dirs) / len(dirs)) if dirs else None,
        per_month=rows,
    )


DEFAULT_STRENGTH = 0.75     # best average across the 11 players in a walk-forward test


def choose_strength(series: dict[str, float], target: str,
                    candidates=(0.0, 0.25, 0.5, 0.75, 1.0), min_history: int = 24):
    """How much of the seasonal swing to keep, chosen by the series' own past errors.

    Each candidate is walked forward through months *before* `target` only, so nothing
    from the target month or later can influence the choice. Returns
    (strength, {strength: (mape, worst)}, basis).
    """
    past = {m: v for m, v in series.items() if m < target and v is not None}
    if len(past) < min_history + 6:
        return DEFAULT_STRENGTH, {}, (f"only {len(past)} months of history, too few to tune; "
                                      f"default strength {DEFAULT_STRENGTH}")
    table = {}
    for c in candidates:
        b = backtest(past, "tune", min_history=min_history, seasonal_strength=c,
                     clip_to_history=False)
        if b.n:
            table[c] = (b.mape, max(x["abs_pct_error"] for x in b.per_month))
    if not table:
        return DEFAULT_STRENGTH, {}, "no usable backtest; default strength"
    # When two strengths are within 3% of each other on past error, the data cannot tell them
    # apart, and the cost of overshooting is larger than undershooting, so take the gentler one.
    floor = min(v[0] for v in table.values())
    best = min(c for c in table if table[c][0] <= floor * 1.03)
    return best, table, (f"lowest walk-forward error over {len(past) - min_history} past months "
                         f"({table[best][0]:.1%} average, {table[best][1]:.0%} worst)")
