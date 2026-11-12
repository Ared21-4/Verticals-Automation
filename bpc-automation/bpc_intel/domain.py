"""Core value types shared by every stage of the pipeline.

The single most important rule in this system is that a number must always
carry how it came to exist.  `Provenance` is that record, and `Value` is the
only container the engine passes around.  Nothing downstream is allowed to
turn an estimate into a reported actual by losing track of where it came from.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class Provenance(str, Enum):
    """How a number came to exist. Ordered loosely from hardest to softest."""

    REPORTED = "reported"          # stated in the player MIS
    CALL_STATED = "call_stated"    # stated explicitly on the expert call
    DERIVED = "derived"            # arithmetic on other values, no judgement
    ESTIMATED = "estimated"        # produced by a model estimator
    CALIBRATED = "calibrated"      # an estimate after bias correction
    ASSUMED = "assumed"            # carried forward / documented assumption
    UNAVAILABLE = "unavailable"    # no evidence; emitted as null

    @property
    def is_actual(self) -> bool:
        """Only MIS actuals may ever be presented as reported."""
        return self is Provenance.REPORTED


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


@dataclass
class Value:
    """A single number plus everything needed to defend it."""

    value: float | None
    provenance: Provenance
    unit: str = ""
    source: str = ""                  # e.g. "MIS:Sep'26 C7"
    method: str = ""                  # e.g. "Shipped / (1 - cancellation)"
    confidence: Confidence = Confidence.MEDIUM
    notes: list[str] = field(default_factory=list)
    # Set when an estimator produced an uncalibrated value that was later
    # adjusted, so the UI and audit sheet can show both.
    uncalibrated: float | None = None

    def __post_init__(self) -> None:
        if self.value is not None and not isinstance(self.value, (int, float)):
            raise TypeError(f"Value.value must be numeric or None, got {type(self.value)}")

    @property
    def is_null(self) -> bool:
        return self.value is None

    def with_note(self, note: str) -> "Value":
        self.notes.append(note)
        return self

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["provenance"] = self.provenance.value
        d["confidence"] = self.confidence.value
        return d


def null(unit: str = "", reason: str = "no evidence available") -> Value:
    """An explicitly unavailable value. Never a silent zero."""
    return Value(
        value=None,
        provenance=Provenance.UNAVAILABLE,
        unit=unit,
        method=reason,
        confidence=Confidence.NONE,
    )


@dataclass
class Finding:
    """A validation result, discrepancy or contradiction worth surfacing."""

    code: str
    severity: Severity
    message: str
    detail: str = ""
    metric: str = ""
    blocking: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


@dataclass
class Insight:
    """One extracted item from the expert call, with its evidence class."""

    statement: str
    metric: str = ""
    category: str = ""
    period: str = ""
    kind: str = "qualitative"   # reported | estimate | opinion | forward_looking | inference
    value: float | None = None
    unit: str = ""
    section: str = ""
    confidence: Confidence = Confidence.MEDIUM
    limitations: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["confidence"] = self.confidence.value
        return d


@dataclass
class Adjustment:
    """A material numerical adjustment, tied to the business driver it represents.

    `driver` exists so the same real-world cause cannot be counted twice
    through two different evidence levers.
    """

    metric: str
    driver: str
    before: float | None
    after: float | None
    rationale: str
    source: str

    @property
    def delta_pct(self) -> float | None:
        if self.before in (None, 0) or self.after is None:
            return None
        return self.after / self.before - 1.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["delta_pct"] = self.delta_pct
        return d


# A row index standing for "this player's sheet has no such metric". Writes to it
# are dropped and reads from it return None, so a player missing a metric
# produces an explicit null instead of a crash or a fabricated number.
ABSENT_ROW = -1

# Slots for metrics a player's sheet has no row for, so a *derivable* value can
# still be computed and reported. They hold no history, so nothing can be
# estimated into them.
VIRTUAL_ROW_BASE = 1_000_000


def is_sheet_row(row: int | None) -> bool:
    """True only for a row the player's own sheet actually carries."""
    return row is not None and 0 < row < VIRTUAL_ROW_BASE


class MetricRows(dict):
    """Metric name -> row, where an unknown metric resolves to ABSENT_ROW."""

    def __missing__(self, key: str) -> int:
        return ABSENT_ROW

    def has(self, key: str) -> bool:
        """Whether the player's sheet carries this metric with real history."""
        return is_sheet_row(dict.get(self, key, ABSENT_ROW))

    def slot(self, key: str) -> bool:
        """Whether there is anywhere to put this metric, real or virtual."""
        return dict.get(self, key, ABSENT_ROW) != ABSENT_ROW


def month_key(d: _dt.date | _dt.datetime | str) -> str:
    """Canonical month identifier, e.g. '2026-09'."""
    if isinstance(d, str):
        return d[:7]
    return f"{d.year:04d}-{d.month:02d}"


def month_label(key: str) -> str:
    """'2026-09' -> "Sep'26", matching the workbook's own headers."""
    y, m = key.split("-")
    name = _dt.date(int(y), int(m), 1).strftime("%b")
    return f"{name}'{y[2:]}"


def shift_months(key: str, n: int) -> str:
    y, m = (int(x) for x in key.split("-"))
    total = y * 12 + (m - 1) + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"
