"""Evidence weighting: availability, quality discounting and renormalisation.

Configured weights say how much each source *would* matter. Effective weights
say how much it matters on this run, after removing sources that are absent and
discounting ones that are present but weak. Both are kept so the audit trail can
show the difference.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from ..domain import Confidence


@dataclass
class SourceState:
    key: str
    label: str
    configured: float
    available: bool
    quality_multiplier: float = 1.0
    quality_reasons: list[str] = field(default_factory=list)
    effective: float = 0.0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WeightResolution:
    sources: dict[str, SourceState]
    available_configured_weight: float
    confidence: Confidence
    warnings: list[str] = field(default_factory=list)

    @property
    def effective(self) -> dict[str, float]:
        return {k: s.effective for k, s in self.sources.items()}

    def table(self) -> list[dict[str, Any]]:
        return [
            {
                "source": s.label,
                "key": s.key,
                "configured_pct": round(s.configured * 100, 2),
                "available": s.available,
                "quality_multiplier": round(s.quality_multiplier, 3),
                "effective_pct": round(s.effective * 100, 2),
                "reasons": "; ".join(s.quality_reasons),
                "note": s.note,
            }
            for s in sorted(self.sources.values(), key=lambda x: -x.configured)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sources": {k: s.to_dict() for k, s in self.sources.items()},
            "available_configured_weight": self.available_configured_weight,
            "confidence": self.confidence.value,
            "warnings": list(self.warnings),
        }


def resolve_weights(
    weight_cfg: dict,
    availability: dict[str, bool],
    quality: dict[str, list[tuple[str, float]]] | None = None,
) -> WeightResolution:
    """Build effective weights from configured weights + what this run actually has.

    `quality` maps a source key to a list of (reason, multiplier) penalties, so a
    source that is present but stale or internally inconsistent contributes less
    without being treated as missing.
    """
    quality = quality or {}
    raw = weight_cfg["weights"]
    total_configured = sum(v["weight"] for v in raw.values())
    if abs(total_configured - 1.0) > 1e-6:
        raise ValueError(
            f"Configured weights sum to {total_configured:.4f}, not 1.0. "
            "Fix the weight configuration before running."
        )

    sources: dict[str, SourceState] = {}
    for key, spec in raw.items():
        avail = bool(availability.get(key, False))
        mult, reasons = 1.0, []
        for reason, m in quality.get(key, []):
            mult *= m
            reasons.append(f"{reason} (x{m:g})")
        sources[key] = SourceState(
            key=key, label=spec.get("label", key), configured=float(spec["weight"]),
            available=avail, quality_multiplier=mult if avail else 0.0,
            quality_reasons=reasons if avail else [],
            note="" if avail else "source not supplied for this run",
        )

    # Redistribute proportionally across what remains, preserving relative weight.
    pool = sum(s.configured * s.quality_multiplier for s in sources.values() if s.available)
    warnings: list[str] = []
    if pool <= 0:
        warnings.append(
            "No evidence source is available. No estimate can be produced from weighted evidence.")
        for s in sources.values():
            s.effective = 0.0
    else:
        for s in sources.values():
            s.effective = (s.configured * s.quality_multiplier / pool) if s.available else 0.0

    available_configured = sum(s.configured for s in sources.values() if s.available)
    thr = weight_cfg.get("confidence_thresholds", {"high": 0.75, "medium": 0.5})
    if available_configured >= thr["high"]:
        conf = Confidence.HIGH
    elif available_configured >= thr["medium"]:
        conf = Confidence.MEDIUM
    elif available_configured > 0:
        conf = Confidence.LOW
    else:
        conf = Confidence.NONE

    missing = [s.label for s in sources.values() if not s.available and s.configured > 0]
    if missing:
        warnings.append(
            f"{len(missing)} of {sum(1 for s in sources.values() if s.configured > 0)} evidence sources unavailable "
            f"({available_configured:.0%} of configured weight present): {', '.join(missing)}. "
            "Remaining weights were renormalised and run confidence reduced."
        )
    for s in sources.values():
        if s.available and s.quality_multiplier < 1.0:
            warnings.append(
                f"{s.label} present but discounted to x{s.quality_multiplier:g}: "
                f"{'; '.join(s.quality_reasons)}")

    return WeightResolution(
        sources=sources,
        available_configured_weight=available_configured,
        confidence=conf,
        warnings=warnings,
    )
