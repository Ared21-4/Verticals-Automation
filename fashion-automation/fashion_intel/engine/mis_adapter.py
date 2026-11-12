"""Reads a MIS laid out on the standard fashion template.

Myntra's MIS is its own shape and has a tuned reader. Every other player is
expected to send the standard template: Overall, Category GMV, Category ASP,
Payment Split, Geographic Split, Price Buckets. This module turns that into
numbers the estimator can use, and says out loud everything it had to decide:

* **Units.** The template carries no unit label, and players send rupees as often
  as crore. The scale is chosen by comparing the MIS gross GMV with the player's
  own prior-month figure, and the choice is reported.
* **Zeros.** A platform cannot have zero GMV, so a zero in a level metric means
  "not supplied". It is treated as missing and listed, never used as a value.
* **Labels.** Metrics are matched by normalised label against configured aliases.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from ..domain import Finding, Severity
from ..readers.mis import MisData


def norm(s: object) -> str:
    return re.sub(r"\s+", " ", str(s).replace("’", "'").strip().lower())


@dataclass
class MisCategory:
    label: str
    gmv_cr: float | None
    asp: float | None
    pars: dict


@dataclass
class GenericMis:
    scale: float
    scale_basis: str
    overall: dict[str, float | None]
    categories: list[MisCategory]
    buckets: dict[int, float]                 # par_id -> INR Cr
    unsplit: list[tuple[str, float, list[int]]]
    zero_fields: list[str]
    unmapped: list[str]
    findings: list[Finding] = field(default_factory=list)
    # growth the MIS states beside a metric: key -> (MoM, YoY). Zero means not supplied.
    growth: dict = field(default_factory=dict)

    @property
    def has_categories(self) -> bool:
        return any(c.gmv_cr for c in self.categories)

    @property
    def category_total_cr(self) -> float:
        return sum(c.gmv_cr for c in self.categories if c.gmv_cr)


def detect_scale(sample: float | None, reference: float | None,
                 candidates=(1.0, 1e7)) -> tuple[float, str]:
    """Divisor that turns the MIS's GMV figures into INR Cr."""
    if not sample or sample <= 0:
        return 1.0, "no GMV figure to size, assumed to be INR Cr already"
    if reference and reference > 0:
        best = min(candidates, key=lambda c: abs(math.log10(sample / c / reference)))
        off = abs(math.log10(sample / best / reference))
        if off <= 1.0:
            return best, (f"{sample:,.0f} / {best:,.0f} = {sample / best:,.1f} Cr, within "
                          f"{10 ** off:.1f}x of the prior month's {reference:,.1f} Cr")
    if sample >= 1e8:
        return 1e7, f"{sample:,.0f} is above 100 million, so it is read as rupees"
    return 1.0, "assumed to be INR Cr already (no history close enough to confirm)"


def _unit_name(scale: float) -> str:
    return {1.0: "INR Cr", 10.0: "INR million", 100.0: "INR lakh", 1e7: "rupees"}.get(
        scale, f"units of {scale:,.0f}")


def read_generic_mis(mis: MisData, month: str, cfg: dict,
                     reference_gross_cr: float | None) -> GenericMis:
    g = cfg["mis_generic"]
    zero_missing = g.get("zero_means_missing", True)
    cells = {(norm(s), norm(m)): c for (s, m), c in mis.cells.get(month, {}).items()}
    zero_fields: list[str] = []
    findings: list[Finding] = []

    def val(section: str, labels: list[str]) -> float | None:
        for lab in labels:
            c = cells.get((norm(section), norm(lab)))
            if c is None:
                continue
            if c.value is None:
                return None
            if c.value == 0 and zero_missing:
                zero_fields.append(f"{section} / {c.metric}")
                return None
            return float(c.value)
        return None

    overall = {k: val("Overall", labels) for k, labels in g["overall"].items()}

    def growth_of(labels: list[str]):
        for lab in labels:
            c = cells.get((norm("Overall"), norm(lab)))
            if c is not None:
                return (c.mom or None, c.yoy or None)
        return (None, None)

    growth = {k: growth_of(g["overall"][k]) for k in ("gross_gmv", "shipped_gmv", "aov", "asp")
              if k in g["overall"]}

    sample = overall.get("gross_gmv") or overall.get("shipped_gmv")
    if sample is None:
        raw = [c.value for (s, _), c in cells.items()
               if s == norm("Category GMV") and c.value]
        sample = max(raw) if raw else None
    scale, basis = detect_scale(sample, reference_gross_cr, tuple(g.get(
        "scale_candidates", (1.0, 1e7))))
    if g.get("gmv_scale") not in (None, "auto"):
        scale, basis = float(g["gmv_scale"]), "set explicitly in the player configuration"

    for k in ("gross_gmv", "shipped_gmv"):
        if overall.get(k) is not None:
            overall[k] = overall[k] / scale

    # ---- categories --------------------------------------------------------
    cat_map = {norm(k): v for k, v in g["categories"].items() if not k.startswith("_")}
    asp_cells = {norm(m): c for (s, m), c in cells.items() if s == norm("Category ASP")}
    cats: list[MisCategory] = []
    unmapped: list[str] = []
    for (s, m), c in cells.items():
        if s != norm("Category GMV"):
            continue
        spec = cat_map.get(m)
        raw = c.value or 0.0
        if spec is None:
            if raw:
                unmapped.append(c.metric)
            continue
        if raw == 0 and zero_missing:
            zero_fields.append(f"Category GMV / {c.metric}")
            gmv = None
        else:
            gmv = raw / scale
        a = asp_cells.get(m)
        asp = None
        if a is not None and a.value:
            asp = float(a.value)
        elif a is not None and a.value == 0 and zero_missing:
            zero_fields.append(f"Category ASP / {a.metric}")
        cats.append(MisCategory(label=c.metric, gmv_cr=gmv, asp=asp, pars=spec))

    # ---- price buckets -----------------------------------------------------
    buckets: dict[int, float] = {}
    unsplit: list[tuple[str, float, list[int]]] = []
    bmap = {norm(k): v for k, v in g.get("price_buckets", {}).items() if not k.startswith("_")}
    ubmap = {norm(k): v for k, v in g.get("unsplit_buckets", {}).items() if not k.startswith("_")}
    for (s, m), c in cells.items():
        if not s.startswith("price buckets") or not c.value:
            continue
        if m in bmap:
            buckets[int(bmap[m])] = c.value / scale
        elif m in ubmap:
            unsplit.append((c.metric, c.value / scale, [int(p) for p in ubmap[m]]))

    gm = GenericMis(scale=scale, scale_basis=basis, overall=overall, categories=cats,
                    buckets=buckets, unsplit=unsplit, zero_fields=zero_fields,
                    unmapped=unmapped, findings=findings, growth=growth)

    # ---- say what was decided ---------------------------------------------
    headline = overall.get("gross_gmv") or overall.get("shipped_gmv")
    findings.append(Finding(
        "MIS_UNITS", Severity.INFO,
        f"MIS GMV figures read as {_unit_name(scale)}"
        + (f"; platform GMV is {headline:,.1f} Cr." if headline else "."),
        f"Scale chosen by comparing against history: {basis}. The template carries no unit "
        "label, so this is detected rather than assumed. Set mis_generic.gmv_scale in the "
        "player configuration to override.", metric="gmv_scale"))
    if zero_fields:
        findings.append(Finding(
            "MIS_ZERO_AS_MISSING", Severity.INFO,
            f"{len(zero_fields)} MIS fields are zero and were treated as not supplied.",
            "A platform cannot have zero GMV, so a zero here means the cell was not filled in. "
            "These fall back to history rather than being used as zero: "
            + ", ".join(zero_fields[:12]) + (" …" if len(zero_fields) > 12 else "")))
    if unmapped:
        findings.append(Finding(
            "MIS_UNMAPPED_CATEGORY", Severity.WARNING,
            f"{len(unmapped)} MIS categories have no mapping and were not used: "
            f"{', '.join(unmapped)}.",
            "Add them to mis_generic.categories in the base configuration."))

    ref_total = overall.get("gross_gmv") or overall.get("shipped_gmv")
    if gm.has_categories and ref_total:
        tot = gm.category_total_cr
        gap = tot / ref_total - 1
        if abs(gap) > 0.02:
            findings.append(Finding(
                "MIS_CATEGORY_RECONCILIATION", Severity.WARNING,
                f"MIS category GMV sums to {tot:,.1f} Cr against a platform GMV of "
                f"{ref_total:,.1f} Cr ({gap:+.1%}).",
                "The categories and the headline disagree. The platform figure sets the level "
                f"and the category mix is rescaled by {ref_total / tot:.4f} to fit it. "
                "Check which one is correct.", metric="category_gmv"))
        else:
            findings.append(Finding(
                "MIS_CATEGORY_RECONCILIATION", Severity.INFO,
                f"MIS category GMV ({tot:,.1f} Cr) reconciles to the platform figure "
                f"({gap:+.2%}).", metric="category_gmv"))
    return gm


def headline_check(mis: MisData, month: str, cfg: dict, ref) -> tuple[float | None, float | None, float, str]:
    """The MIS's platform GMV in INR Cr, the player's prior-month figure, and the unit scale.

    Used for every player: it is how a MIS sent in rupees is recognised, and how a
    file that belongs to another player (or another month) is caught.
    """
    from ..domain import shift_months
    g = cfg.get("mis_generic") or {}
    P = cfg.get("platform_rows") or {}
    cells = {(norm(s_), norm(m_)): c for (s_, m_), c in mis.cells.get(month, {}).items()}

    def first(labels):
        for lab in labels:
            c = cells.get((norm("Overall"), norm(lab)))
            if c is not None and c.value:
                return float(c.value)
        return None

    labels = (g.get("overall") or {})
    prev = shift_months(month, -1)

    def prior(key):
        row = P.get(key)
        return ref.value(row, prev) if row else None

    gross = first(labels.get("gross_gmv", ["Gross GMV at SP"]))
    shipped = first(labels.get("shipped_gmv", ["Shipped GMV"]))
    if gross:
        sample, refv = gross, prior("gross_gmv")
    elif shipped:
        sample, refv = shipped, prior("shipped_gmv") or prior("gross_gmv")
    else:
        vals = [c.value for (s_, _), c in cells.items() if s_ == norm("Category GMV") and c.value]
        sample, refv = (sum(vals) if vals else None), prior("shipped_gmv") or prior("gross_gmv")
    if not sample:
        return None, refv, 1.0, "no GMV figure in the MIS"
    scale, basis = detect_scale(sample, refv, tuple(g.get("scale_candidates", (1.0, 1e7))))
    return sample / scale, refv, scale, basis
