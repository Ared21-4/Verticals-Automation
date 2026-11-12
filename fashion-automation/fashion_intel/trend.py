"""Trend and deviation for the headline metrics, against the reference's own history.

For each metric: this month's figure, last month, the same month last year, the recent
average, and, when the reference already holds a number for this month, how far this run
sits from it. Every difference is stated; a flag marks the ones worth a second look.

Nothing here changes a figure. It reads the reference as supplied, which is why uploading
a reference that carries the previous numbers sharpens it.
"""

from __future__ import annotations

from typing import Any

from .domain import month_label, shift_months

# label, par_id, unit
HEADLINE = [
    ("Gross GMV", 241, "INR Cr"), ("Shipped GMV", 243, "INR Cr"), ("Net GMV", 247, "INR Cr"),
    ("Cancellation", 242, "%"), ("Returns", 244, "%"),
    ("AOV", 240, "INR"), ("ASP", 468, "INR"), ("Basket size", 519, "#"),
    ("Monthly transacting users", 238, "Mn"),
    ("Fashion GMV", 457, "INR Cr"), ("Apparel GMV", 554, "INR Cr"),
]

# A move bigger than this is flagged for a look. Not an error: sales and festivals move GMV.
MOM_FLAG, YOY_FLAG, REF_FLAG = 0.25, 0.60, 0.10


def _chg(cur: float | None, base: float | None, unit: str) -> float | None:
    if cur is None or base is None:
        return None
    if unit == "%":
        return cur - base                      # percentage points
    return (cur / base - 1.0) if base else None


def trend_rows(est, ref, cfg: dict, month: str) -> list[dict[str, Any]]:
    """One row per headline metric that has a value this month."""
    prior, yoy_m = shift_months(month, -1), shift_months(month, -12)
    recent = [shift_months(month, -i) for i in (1, 2, 3)]
    out: list[dict[str, Any]] = []
    for label, par, unit in HEADLINE:
        v = est.value_for_par(ref, par)
        if v is None or v.is_null:
            continue
        row = ref.par_to_row.get(par)
        hist = (lambda m: ref.value(row, m)) if row else (lambda m: None)

        last, year = hist(prior), hist(yoy_m)
        pts = [x for x in (hist(m) for m in recent) if x is not None]
        avg = sum(pts) / len(pts) if len(pts) >= 2 else None
        stored = hist(month)                        # what the reference held for this month
        mom, yoy = _chg(v.value, last, unit), _chg(v.value, year, unit)
        vs_avg, dev = _chg(v.value, avg, unit), _chg(v.value, stored, unit)

        flags = []
        pct = unit != "%"
        if mom is not None and ((abs(mom) > MOM_FLAG) if pct else (abs(mom) > 0.05)):
            flags.append("big move on last month")
        if yoy is not None and ((abs(yoy) > YOY_FLAG) if pct else (abs(yoy) > 0.10)):
            flags.append("big move on last year")
        if dev is not None and ((abs(dev) > REF_FLAG) if pct else (abs(dev) > 0.03)):
            flags.append("differs from reference")
        rec = getattr(est, "reconciliations", {}).get(par)
        balanced = bool(rec and rec.final_level and rec.level
                        and abs(rec.final_level / rec.level - 1) > 1e-9)
        if balanced:
            flags.append(f"balanced against stored history (MIS reports {rec.level:,.1f})")
        elif rec is not None and rec.diagnosis in ("year_ago_base", "prior_month_base", "level"):
            flags.append("stored history disagrees with the stated growth")
        use_rec = rec is not None and not balanced
        out.append({
            "balanced": balanced,
            "mis_level": rec.level if balanced else None,
            "year_ago_reconciled": rec.year_ago_reconciled if use_rec and rec.year_ago_basis != "stored history" else None,
            "yoy_reconciled": rec.yoy_reconciled if use_rec and rec.year_ago_basis != "stored history" else None,
            "prior_reconciled": rec.prior_month_reconciled if use_rec and rec.prior_month_basis != "stored history" else None,
            "mom_reconciled": rec.mom_reconciled if use_rec and rec.prior_month_basis != "stored history" else None,
            "base_basis": (rec.year_ago_basis if rec and rec.year_ago_basis != "stored history" else
                           rec.prior_month_basis if rec and rec.prior_month_basis != "stored history" else ""),
            "yoy_stated": rec.yoy_stated if rec else None, "mom_stated": rec.mom_stated if rec else None,
            "diagnosis": rec.diagnosis if rec else "",
            "metric": label, "par_id": par, "unit": unit, "value": v.value,
            "source": v.provenance.value, "prior_month": last, "mom": mom,
            "year_ago": year, "yoy": yoy, "recent_avg": avg, "vs_recent_avg": vs_avg,
            "reference_figure": stored, "vs_reference": dev,
            "flag": "; ".join(flags) if flags else "",
            "pp": unit == "%",
        })
    return out


def reference_months(ref) -> str:
    """'Jan'22 to Sep'26', for stating what history the comparison rests on."""
    return f"{month_label(ref.months[0])} to {month_label(ref.months[-1])}" if ref.months else "none"


DEVIATION_METRICS = [("Gross GMV", 241), ("Shipped GMV", 243), ("Net GMV", 247), ("AOV", 240),
                     ("Gross transactions (Mn)", 239), ("Fashion GMV", 457), ("Apparel GMV", 554)]


def balance_view(est, mis_only, ref, past: list[dict], month: str) -> dict[str, Any]:
    """How the output sits against the MIS: this month, earlier months, and by metric."""
    rc = getattr(est, "reconciliations", {}).get(241)
    this = None
    if rc is not None and rc.balanced_level is not None:
        this = {
            "month": month, "mis_level": rc.level, "history_level": rc.balanced_level,
            "weight": rc.blend_weight, "weight_basis": rc.weight_basis,
            "output": rc.final_level, "capped": rc.capped,
            "deviation": (rc.final_level / rc.level - 1) if rc.level else None,
            "mis_yoy": rc.yoy_stated, "mis_mom": rc.mom_stated,
            "yoy_mis_level": rc.yoy_stored, "mom_mis_level": rc.mom_stored,
            "yoy_output": rc.yoy_final, "mom_output": rc.mom_final,
            "narrative": rc.narrative,
        }
    earlier = [{"month": p["month"], "mis_level": p.get("mis_level"),
                "history_level": p.get("history_level"), "weight": p.get("weight"),
                "output": p.get("output_level"),
                "deviation": ((p["output_level"] / p["mis_level"] - 1)
                              if p.get("mis_level") and p.get("output_level") else None),
                "history_gap": p.get("history_gap")} for p in past]
    devs = []
    if mis_only is not None:
        for label, par in DEVIATION_METRICS:
            a, b = est.value_for_par(ref, par), mis_only.value_for_par(ref, par)
            if a is None or b is None or a.is_null or b.is_null or not b.value:
                continue
            devs.append({"metric": label, "mis_based": b.value, "output": a.value,
                         "deviation": a.value / b.value - 1})
    return {"this": this, "earlier": earlier, "deviations": devs}
