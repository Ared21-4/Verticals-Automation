"""Independent cross-checks of the MIS against figures the expert gave separately.

The expert call states some category figures in a form that does not depend on
the MIS at all — a base and a growth rate, or a price-point build-up and the
share it represents. Reconstructing a category from those and comparing it with
the reported MIS value tests whether the MIS is right, using evidence the MIS
never touched.

This never changes a number. It produces a verdict on how well two independent
sources agree, which is what tells an analyst whether a deviation against the
stored workbook is the estimate's problem or the workbook's.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from ..domain import Finding, Severity


@dataclass
class Check:
    name: str
    target: str
    expected: float
    actual: float
    method: str
    inputs: str

    @property
    def deviation(self) -> float:
        return self.expected / self.actual - 1.0 if self.actual else float("nan")

    @property
    def verdict(self) -> str:
        d = abs(self.deviation)
        if d <= 0.02:
            return "corroborated"
        if d <= 0.10:
            return "consistent"
        return "conflicting"

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.name, "category": self.target,
            "expected_from_call": self.expected, "reported_in_mis": self.actual,
            "deviation": self.deviation, "verdict": self.verdict,
            "method": self.method, "inputs": self.inputs,
        }


@dataclass
class CorroborationReport:
    checks: list[Check] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.checks)

    @property
    def mean_abs_deviation(self) -> float | None:
        if not self.checks:
            return None
        return statistics.fmean(abs(c.deviation) for c in self.checks)

    @property
    def corroborated(self) -> int:
        return sum(1 for c in self.checks if c.verdict == "corroborated")

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "corroborated": self.corroborated,
            "mean_abs_deviation": self.mean_abs_deviation,
            "checks": [c.to_dict() for c in self.checks],
            "statements": list(self.statements),
        }


def corroborate(cfg: dict, mis_category_gmv: dict[str, float] | None,
                transcript, platform: dict[str, float | None] | None = None
                ) -> tuple[CorroborationReport, list[Finding]]:
    rep = CorroborationReport()
    findings: list[Finding] = []
    platform = platform or {}
    mis_category_gmv = mis_category_gmv or {}
    rules = [r for r in cfg.get("corroboration", []) if not str(r.get("name", "")).startswith("_")]
    if not rules or transcript is None or not (mis_category_gmv or platform):
        rep.statements.append(
            "No independent cross-check of the MIS was possible for this run.")
        return rep, findings

    for rule in rules:
        if rule["method"] == "stated_vs_platform":
            stated = transcript.stated_values.get(rule["label"])
            actual = platform.get(rule["platform_metric"])
            if stated is None or not actual:
                continue
            c = Check(name=rule["name"], target=rule["label"], expected=stated, actual=actual,
                      method=rule["method"],
                      inputs=f"the expert confirmed {rule['label']} of about {stated:,.0f} Cr")
            rep.checks.append(c)
            findings.append(Finding(
                "MIS_CORROBORATION",
                Severity.INFO if c.verdict != "conflicting" else Severity.WARNING,
                f"{rule['label']}: the call states about {stated:,.0f} Cr against "
                f"{actual:,.1f} Cr in the data ({c.deviation:+.2%}), {c.verdict}.",
                "The expert's figure was given on the call, independently of the MIS file.",
                metric=rule["label"]))
            continue

        target = rule.get("mis_category")
        actual = mis_category_gmv.get(target)
        if actual is None:
            continue
        expected = inputs = None

        if rule["method"] == "base_times_yoy":
            base = transcript.stated_values.get(rule["base_label"]) \
                or transcript.stated_bases.get(rule.get("base_key", ""))
            yoy = transcript.category_yoy.get(rule["yoy_category"])
            if base is None or yoy is None:
                continue
            expected = base * (1 + yoy)
            inputs = (f"expert base {base:,.0f} Cr for {rule['yoy_category']} "
                      f"x (1 {yoy:+.0%} YoY)")

        elif rule["method"] == "parts_over_share":
            parts, missing = [], False
            for label in rule["part_labels"]:
                pp = transcript.price_points.get(label)
                if pp is None:
                    missing = True
                    break
                parts.append(pp["current"])
            share = transcript.stated_shares.get(rule["share_label"])
            if missing or share in (None, 0):
                continue
            expected = sum(parts) / share
            inputs = (f"expert price points {' + '.join(f'{p:,.0f}' for p in parts)} Cr "
                      f"= {sum(parts):,.0f} Cr, stated at {share:.0%} of the category")

        if expected is None:
            continue

        c = Check(name=rule["name"], target=target, expected=expected, actual=actual,
                  method=rule["method"], inputs=inputs)
        rep.checks.append(c)
        sev = {"corroborated": Severity.INFO, "consistent": Severity.INFO,
               "conflicting": Severity.WARNING}[c.verdict]
        findings.append(Finding(
            "MIS_CORROBORATION", sev,
            f"{target}: the call independently implies {expected:,.1f} Cr against a reported "
            f"{actual:,.1f} Cr ({c.deviation:+.2%}) — {c.verdict}.",
            f"Built from {inputs}. This uses no MIS input, so agreement is evidence that the "
            "reported figure is sound.",
            metric=target))

    if rep.checks:
        rep.statements.append(
            f"{rep.corroborated} of {rep.n} independent cross-checks corroborate the MIS within "
            f"2%. Mean absolute deviation across them is {rep.mean_abs_deviation:.2%}.")
        rep.statements.append(
            "These reconstruct a category from figures the expert stated separately and compare "
            "them with the reported value. They use no MIS input, so they test the MIS rather "
            "than repeat it.")
    return rep, findings
