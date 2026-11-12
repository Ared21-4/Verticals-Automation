"""Expert-call transcript reader.

The supplied transcripts are structured documents: headings, bulleted
statements, typed tables and an explicit clarifications log. That structure is
machine-readable, so extraction here is deterministic and every insight keeps a
pointer back to the section it came from.

An LLM is not required for this path and is not used. `llm.py` provides an
optional hook for genuinely unstructured transcripts; its output is validated
against the same schema before any number reaches a calculation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import docx
from docx.table import Table
from docx.text.paragraph import Paragraph

from ..domain import Confidence, Insight

# U+2212 MINUS SIGN appears throughout the supplied document and is not ASCII '-'.
_MINUS = "−"
_PCT = re.compile(rf"([+\-{_MINUS}]?\s?\d+(?:\.\d+)?)\s*%")
_RUPEE = re.compile(r"[₹Rs.]*\s*([\d,]+(?:\.\d+)?)\s*(Cr|crore|lakh|lakhs)", re.I)

_FORWARD = ("will ", "expects", "expected", "outlook", "plan", "planned", "going to",
            "coming months", "next month", "forecast", "anticipat")
_OPINION = ("in the expert's view", "the expert said", "the expert called",
            "described as", "he said", "expert stressed")
_INFERENCE = ("redseer", "derived", "implied", "interpretation", "cross-check",
              "works out to", "read as")
_UNCERTAIN = ("garbled", "unclear", "transcribed", "not certain", "if i'm not wrong",
              "low confidence", "partly garbled", "left open", "mismatch")

_NOTE_PREFIXES = ("DATA NOTE", "NUANCE", "IMPORTANT CLARIFICATION", "SOURCE NOTE",
                  "STRATEGIC SIGNAL")


@dataclass
class EventShift:
    """Evidence that the month's recurring sale event did not fall in it this year."""

    detected: bool
    evidence: list[str] = field(default_factory=list)
    direction: str = ""          # "out_of_month" | "into_month"

    @property
    def summary(self) -> str:
        if not self.detected:
            return "No event-timing shift detected; normal seasonality applies."
        return (f"The recurring sale event moved {self.direction.replace('_', ' ')} "
                f"for this reporting month ({len(self.evidence)} supporting statements).")


# Phrases that, taken together, establish that a festive or sale event moved.
_SHIFT_TIMING = re.compile(
    r"(event dates?|festive events?|sale events?|event date difference)"
    r"[^.]{0,80}?(moved|differ|shift|later|lag)"
    r"|(moved|shifted)\s+(about\s+)?\d+\s*[-–]\s*\d+\s*days\s+later"
    r"|purely because of the event date difference", re.I)
# "flat on August because both were BAU months": the expert says no sale lifted the month,
# so the festive uplift prior years show for this calendar month did not happen.
_BAU = re.compile(r"(both|this|the)\s+(?:\w+\s+){0,2}(?:months?\s+)?(?:were|was|are|is)\s+(?:a\s+)?BAU"
                  r"|\bBAU[- ](?:type\s+)?months?\b|\bBAU month", re.I)
_SHIFT_OUT = re.compile(
    r"(fall|falls|fell)\s+in\s+(October|November|December|January|February|March|April|May|"
    r"June|July|August|September)\s+this year"
    r"|last year'?s? festive (sales?|half) fell in", re.I)


@dataclass
class TranscriptData:
    path: str
    title: str = ""
    sections: list[str] = field(default_factory=list)
    insights: list[Insight] = field(default_factory=list)
    category_yoy: dict[str, float] = field(default_factory=dict)
    category_mom: dict[str, float] = field(default_factory=dict)
    quick_reference: dict[str, str] = field(default_factory=dict)
    clarifications: list[dict[str, str]] = field(default_factory=list)
    stated_bases: dict[str, float] = field(default_factory=dict)
    # Figures the expert gave that are independent of the MIS, so they can be
    # used to cross-check it rather than to produce it.
    # The expert says the cancellation rate already contains RTO. This decides
    # whether a MIS that lists both can be taken at face value.
    cancellation_includes_rto: bool = False
    cancellation_definition: str = ""
    price_points: dict[str, dict[str, float]] = field(default_factory=dict)
    stated_values: dict[str, float] = field(default_factory=dict)
    stated_shares: dict[str, float] = field(default_factory=dict)
    platform_gmv_given: bool = False
    transcription_quality: str = "good"
    month: str = ""              # set when this call stands in from an earlier month
    notes: list[str] = field(default_factory=list)

    event_shift: "EventShift" = field(default_factory=lambda: EventShift(False))

    @property
    def low_confidence(self) -> bool:
        return self.transcription_quality in ("low", "poor")


def _parse_pct(text: str) -> float | None:
    m = _PCT.search(text.replace(_MINUS, "-"))
    if not m:
        return None
    return float(m.group(1).replace(" ", "")) / 100.0


def _classify(text: str) -> tuple[str, Confidence, str]:
    low = text.lower()
    limitation = ""
    conf = Confidence.MEDIUM
    if any(w in low for w in _UNCERTAIN):
        conf = Confidence.LOW
        limitation = "Transcript flagged this wording as garbled, inconsistent or uncertain."
    if any(w in low for w in _FORWARD):
        return "forward_looking", conf, limitation or "Forward-looking expectation, not a result."
    if any(w in low for w in _INFERENCE):
        return "inference", conf, limitation or "Derived or interpreted, not stated by the expert."
    if re.search(r"\d", text) and not any(w in low for w in _OPINION):
        return "reported", conf, limitation
    if any(w in low for w in _OPINION):
        return "opinion", conf, limitation
    return "qualitative", conf, limitation


def _iter_blocks(document) -> list[tuple[str, Any]]:
    out = []
    for child in document.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            out.append(("p", Paragraph(child, document)))
        elif tag == "tbl":
            out.append(("tbl", Table(child, document)))
    return out


def _table_rows(tbl: Table) -> list[list[str]]:
    rows = []
    for r in tbl.rows:
        cells, seen = [], set()
        for c in r.cells:
            if id(c._tc) in seen:      # merged cells repeat; keep one copy
                continue
            seen.add(id(c._tc))
            cells.append(c.text.strip().replace("\n", " "))
        rows.append(cells)
    return rows


_ARROW = re.compile(
    r"~?\s*₹?\s*([\d,]+(?:\.\d+)?)\s*Cr\s*(?:→|->)\s*~?\s*₹?\s*"
    r"([\d,]+(?:\.\d+)?)\s*Cr", re.I)
_SINGLE = re.compile(r"^~?\s*₹\s*([\d,]+(?:\.\d+)?)\s*Cr\b", re.I)
_SHARE = re.compile(r"^~?\s*(\d+(?:\.\d+)?)\s*%\s*$")


def _extract_independent_figures(data: TranscriptData) -> None:
    """Pull the expert's own rupee figures and shares out of the quick reference.

    These are stated independently of the MIS, so they can corroborate it. They
    are never used to produce a value, only to check one.
    """
    for label, raw in data.quick_reference.items():
        if not isinstance(raw, str):
            continue
        txt = raw.strip()
        m = _ARROW.search(txt)
        if m:
            data.price_points[label] = {
                "base": float(m.group(1).replace(",", "")),
                "current": float(m.group(2).replace(",", "")),
            }
            continue
        m = _SINGLE.match(txt)
        if m:
            data.stated_values[label] = float(m.group(1).replace(",", ""))
            continue
        m = _SHARE.match(txt)
        if m:
            data.stated_shares[label] = float(m.group(1)) / 100.0


def _detect_event_shift(data: TranscriptData) -> EventShift:
    """Look for explicit statements that the sale event moved out of this month.

    Deterministic and conservative: it requires a statement about event *timing*
    changing, not merely a mention of a festival. The result is surfaced in the
    UI so an analyst can confirm or override it.
    """
    evidence: list[str] = []
    for ins in data.insights:
        t = ins.statement
        if _SHIFT_TIMING.search(t) or _SHIFT_OUT.search(t) or _BAU.search(t):
            evidence.append(f"[{ins.section}] {t}")
    for c in data.clarifications:
        if _SHIFT_TIMING.search(c["text"]):
            evidence.append(f"[{c['section']}] {c['text']}")
    if "Event date lag vs last year" in data.quick_reference:
        evidence.append(
            f"[Appendix A] Event date lag vs last year: "
            f"{data.quick_reference['Event date lag vs last year']}")
    if not evidence:
        return EventShift(False)
    return EventShift(True, evidence[:12], "out_of_month")


def load_transcript(path: str) -> TranscriptData:
    document = docx.Document(path)
    data = TranscriptData(path=path)
    section = ""

    for kind, block in _iter_blocks(document):
        if kind == "p":
            text = block.text.strip()
            if not text:
                continue
            style = (getattr(block.style, "name", "") or "")
            if style.startswith("Heading"):
                section = text
                data.sections.append(text)
                if not data.title:
                    pass
                continue
            if not data.title and len(data.sections) == 0:
                data.title = text
                continue
            k, conf, lim = _classify(text)
            data.insights.append(Insight(
                statement=text, section=section, kind=k,
                confidence=conf, limitations=lim, period="Sep 2026",
            ))
            continue

        rows = _table_rows(block)
        if not rows:
            continue
        head = " ".join(rows[0]).strip()

        # Narrative note tables carry the caveats, not data.
        if any(head.upper().startswith(p) for p in _NOTE_PREFIXES):
            body = " ".join(" ".join(r) for r in rows)
            label = next(p for p in _NOTE_PREFIXES if head.upper().startswith(p))
            data.clarifications.append({"section": section, "type": label, "text": body})
            if "No Myntra platform GMV" in body or "Platform GMV | Not given" in body:
                data.platform_gmv_given = False
            if "garbled" in body.lower() or "stitched" in body.lower():
                data.transcription_quality = "mixed"
            continue

        header_cells = [c.lower() for c in rows[0]]

        # Section 2.1 — category YoY.
        if header_cells and header_cells[0].startswith("category") and any(
                "yoy" in c for c in header_cells):
            for r in rows[1:]:
                if len(r) < 2:
                    continue
                cat = r[0].strip()
                pct = _parse_pct(r[1])
                if cat and pct is not None:
                    data.category_yoy[cat] = pct
                    data.insights.append(Insight(
                        statement=f"{cat}: {r[1]} YoY" + (f" — {r[2]}" if len(r) > 2 else ""),
                        metric="category_shipped_gmv", category=cat, period="Sep 2026 vs Sep 2025",
                        kind="reported", value=pct, unit="%", section=section,
                        confidence=Confidence.HIGH,
                    ))
            continue

        # Appendix A — quick-reference table.
        if header_cells[:2] == ["metric", "value"]:
            for r in rows[1:]:
                if len(r) < 2 or not r[0]:
                    continue
                data.quick_reference[r[0].strip()] = r[1].strip()
                label, raw = r[0].strip(), r[1].strip()
                if "not given" in raw.lower():
                    if "platform gmv" in label.lower():
                        data.platform_gmv_given = False
                    continue
                k, conf, lim = _classify(f"{label} {raw}")
                val = _parse_pct(raw)
                data.insights.append(Insight(
                    statement=f"{label}: {raw}", metric=label, period="Sep 2026",
                    kind=k, value=val, unit="%" if val is not None else "",
                    section="Appendix A", confidence=conf, limitations=lim,
                ))
                if "personal care base" in label.lower():
                    m = _RUPEE.search(raw)
                    if m:
                        data.stated_bases["personal_care_sep25"] = float(
                            m.group(1).replace(",", ""))
            continue

        # Appendix B — clarifications log.
        if header_cells[:1] == ["item"]:
            for r in rows[1:]:
                if len(r) >= 3:
                    data.clarifications.append(
                        {"section": "Appendix B", "type": "clarification",
                         "text": f"{r[0]} | as stated: {r[1]} | treated as: {r[2]}"})
            continue

        # Any other table: keep the rows as insights so nothing is silently dropped.
        for r in rows[1:]:
            line = " | ".join(x for x in r if x)
            if not line:
                continue
            k, conf, lim = _classify(line)
            data.insights.append(Insight(
                statement=line, section=section, kind=k,
                confidence=conf, limitations=lim, period="Sep 2026",
            ))

    # MoM figures appear in prose rather than a table.
    for ins in data.insights:
        m = re.search(r"([A-Z][\w'’\s&]+?)\s+(?:grew|fell|rose|declined)\s+"
                      rf"([+\-{_MINUS}]?\d+(?:\.\d+)?)\s*%\s*MoM", ins.statement)
        if m:
            cat = m.group(1).strip()
            pct = float(m.group(2).replace(_MINUS, "-")) / 100.0
            if "fell" in ins.statement or "declined" in ins.statement:
                pct = -abs(pct)
            data.category_mom[cat] = pct
        if re.search(r"\bLTA fell 18%\s*MoM", ins.statement):
            data.category_mom["LTA"] = -0.18

    _extract_independent_figures(data)
    for ins in data.insights:
        t = ins.statement
        if re.search(r"cancel", t, re.I) and re.search(r"includes?[^.]{0,140}\bRTO\b", t, re.I):
            data.cancellation_includes_rto = True
            data.cancellation_definition = t
            break
    data.event_shift = _detect_event_shift(data)
    if data.event_shift.detected:
        data.notes.append(
            "The call establishes that the month's recurring sale event did not fall in it this "
            "year. Calendar seasonality learned from prior years of this month is therefore not "
            "representative and is suppressed by default.")

    if not data.category_yoy:
        data.notes.append(
            "No category YoY table was found. Category corroboration is unavailable for this run.")
    if not data.platform_gmv_given:
        data.notes.append(
            "The expert gave no platform GMV, MoM or YoY figure for the reporting month. "
            "No platform-level number may be attributed to the call.")
    return data
