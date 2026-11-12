"""Reconcile a level with the growth rates stated beside it.

A MIS (or a call) gives a figure and also says how it moved: month-on-month and year-on-year.
Those growth rates are measured against the *stored* history. When they do not square with
the level, something is wrong, and the useful question is which input.

Three things are in play for each metric:

    level L              what the MIS or call reports for the month
    MoM-implied level    stored prior month x (1 + stated MoM)
    YoY-implied level    stored year-ago month x (1 + stated YoY)

If L and the MoM-implied level agree but the YoY-implied level does not, the stored year-ago
month is the odd one out, not L. Forcing L to the YoY-implied figure would break the MoM
that already agrees, which is the failure this exists to avoid. Instead the level stays and
the base is corrected, using an independent figure (the expert's own year-ago number) where
there is one, and what the stated growth implies where there is not.

Nothing here edits stored history. It reports a reconciled base beside the stored one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

TOLERANCE = 0.05        # agreement within 5% counts as consistent


@dataclass
class Reconciliation:
    metric: str
    level: float
    level_source: str
    # stored history
    prior_month: float | None = None
    year_ago: float | None = None
    # stated growth
    mom_stated: float | None = None
    yoy_stated: float | None = None
    # tests
    mom_implied_level: float | None = None
    yoy_implied_level: float | None = None
    mom_gap: float | None = None            # implied level vs the reported level
    yoy_gap: float | None = None
    diagnosis: str = "insufficient"
    # reconciled bases and the growth measured against them
    prior_month_reconciled: float | None = None
    year_ago_reconciled: float | None = None
    year_ago_basis: str = ""
    prior_month_basis: str = ""
    mom_stored: float | None = None
    yoy_stored: float | None = None
    mom_reconciled: float | None = None
    yoy_reconciled: float | None = None
    # a level nudge, only ever for a level that is not itself reported
    adjusted_level: float | None = None
    # the history balance: where the stored history says the level should sit
    balanced_level: float | None = None     # growth-consistent level on the stored history
    blend_weight: float | None = None       # how far the output moves toward it
    weight_basis: str = ""
    final_level: float | None = None        # what the output carries
    capped: bool = False
    mom_final: float | None = None          # growth of the final level on the stored history
    yoy_final: float | None = None
    narrative: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.1%}"


def reconcile_growth(
    metric: str,
    level: float,
    level_source: str,
    prior_month: float | None,
    year_ago: float | None,
    mom_stated: float | None = None,
    yoy_stated: float | None = None,
    call_year_ago: float | None = None,
    level_is_reported: bool = True,
    tol: float = TOLERANCE,
    label_prior: str = "last month",
    label_year: str = "last year",
) -> Reconciliation:
    r = Reconciliation(metric=metric, level=level, level_source=level_source,
                       prior_month=prior_month, year_ago=year_ago,
                       mom_stated=mom_stated, yoy_stated=yoy_stated)
    if not level:
        return r

    r.mom_stored = (level / prior_month - 1) if prior_month else None
    r.yoy_stored = (level / year_ago - 1) if year_ago else None
    if prior_month and mom_stated is not None:
        r.mom_implied_level = prior_month * (1 + mom_stated)
        r.mom_gap = r.mom_implied_level / level - 1
    if year_ago and yoy_stated is not None:
        r.yoy_implied_level = year_ago * (1 + yoy_stated)
        r.yoy_gap = r.yoy_implied_level / level - 1

    ok_m = r.mom_gap is not None and abs(r.mom_gap) <= tol
    ok_y = r.yoy_gap is not None and abs(r.yoy_gap) <= tol
    have_m, have_y = r.mom_gap is not None, r.yoy_gap is not None

    # default: the stored bases stand
    r.prior_month_reconciled, r.year_ago_reconciled = prior_month, year_ago
    r.prior_month_basis = r.year_ago_basis = "stored history"

    if not (have_m or have_y):
        r.diagnosis = "insufficient"
        r.narrative = "No MoM or YoY was stated alongside the level, so there is nothing to reconcile."
    elif (ok_m or not have_m) and (ok_y or not have_y):
        r.diagnosis = "consistent"
        r.narrative = ("The stated growth agrees with the reported level on the stored history "
                       f"(MoM gap {_pct(r.mom_gap)}, YoY gap {_pct(r.yoy_gap)}).")
    elif have_y and not ok_y and (ok_m or not have_m):
        r.diagnosis = "year_ago_base"
        if call_year_ago:
            r.year_ago_reconciled, r.year_ago_basis = call_year_ago, \
                "the expert's own figure for that month, stated on the call"
        else:
            r.year_ago_reconciled = level / (1 + yoy_stated)
            r.year_ago_basis = "implied by the stated YoY and the reported level"
        r.narrative = (
            f"The reported {level:,.1f} fits the stated MoM ({_pct(r.mom_gap)} gap against "
            f"{label_prior}) but not the stated YoY: on the stored {label_year} figure of "
            f"{year_ago:,.1f} it gives {_pct(r.yoy_stored)}, not {_pct(yoy_stated)}. "
            f"The stored {label_year} figure is the inconsistent input. For the stated YoY it "
            f"would have to be {level / (1 + yoy_stated):,.1f}"
            + (f", and the call independently puts it near {call_year_ago:,.0f}." if call_year_ago
               else ".")
            + " The level is not moved: forcing it to the YoY on the stored base would give "
              f"{r.yoy_implied_level:,.1f} and break the MoM.")
    elif have_m and not ok_m and (ok_y or not have_y):
        r.diagnosis = "prior_month_base"
        r.prior_month_reconciled = level / (1 + mom_stated)
        r.prior_month_basis = "implied by the stated MoM and the reported level"
        r.narrative = (
            f"The reported {level:,.1f} fits the stated YoY but not the stated MoM: on the "
            f"stored {label_prior} figure of {prior_month:,.1f} it gives {_pct(r.mom_stored)}, "
            f"not {_pct(mom_stated)}. The stored {label_prior} figure is the inconsistent input; "
            f"it would have to be {r.prior_month_reconciled:,.1f}.")
    else:
        r.diagnosis = "level"
        r.narrative = (
            f"The reported {level:,.1f} disagrees with both stated growth rates on the stored "
            f"history (MoM gap {_pct(r.mom_gap)}, YoY gap {_pct(r.yoy_gap)}). Either the level "
            "or both stored bases are off, and there is no independent figure to say which.")
        if not level_is_reported:
            cands = [(level, 0.5)]
            for x in (r.mom_implied_level, r.yoy_implied_level):
                if x:
                    cands.append((x, 0.25))
            tot = sum(w for _, w in cands)
            r.adjusted_level = sum(v * w for v, w in cands) / tot
            r.narrative += (f" The level is not a reported figure, so it is placed between the "
                            f"evidence at {r.adjusted_level:,.1f} (the level weighted 50%, each "
                            "growth-implied level 25%).")
        else:
            r.narrative += " A reported figure is never moved; the conflict is flagged."

    if r.year_ago_reconciled:
        r.yoy_reconciled = level / r.year_ago_reconciled - 1
    if r.prior_month_reconciled:
        r.mom_reconciled = level / r.prior_month_reconciled - 1
    return r


# ---------------------------------------------------------------- history balance

def balanced_level(prior_month: float | None, year_ago: float | None,
                   mom_stated: float | None, yoy_stated: float | None) -> float | None:
    """The level whose MoM and YoY, on the stored history, are jointly closest to the stated ones.

    Minimises (L/year_ago - (1+YoY))^2 + (L/prior - (1+MoM))^2, which has a closed form. With
    one growth rate it is simply the level that rate implies; with two it sits between them, so
    neither growth rate ends up far off while the other is matched.
    """
    terms = []
    if year_ago and yoy_stated is not None:
        terms.append((year_ago, 1 + yoy_stated))
    if prior_month and mom_stated is not None:
        terms.append((prior_month, 1 + mom_stated))
    if not terms:
        return None
    return sum(g / b for b, g in terms) / sum(1 / (b * b) for b, _ in terms)


def trusted_weight(base: float, past_gaps: list[float], max_gap: float = 0.20,
                   floor: float = 0.25) -> tuple[float, str]:
    """How much weight the history gets, learned from how it has behaved.

    `past_gaps` are, for earlier saved months, how far the history-balanced level sat from
    the MIS level. If history keeps landing far from the MIS, it is a poor guide and is
    weighted less; if it keeps landing close, it is trusted. This is what narrows the gap
    between the MIS and the output from one month to the next.
    """
    if not past_gaps:
        return base, "no earlier saved months to learn from"
    g = sum(past_gaps[-3:]) / len(past_gaps[-3:])
    w = max(min(base, floor), base * (1 - min(1.0, g / max_gap)))
    return w, (f"history sat {g:.1%} from the MIS on average over the last "
               f"{len(past_gaps[-3:])} saved month(s)")


def blend(level: float, target: float, weight: float, cap: float) -> tuple[float, bool]:
    """Move `level` a fraction `weight` of the way to `target`, by no more than `cap` of the level."""
    move = weight * (target - level)
    limit = cap * level
    capped = abs(move) > limit
    if capped:
        move = limit if move > 0 else -limit
    return level + move, capped
