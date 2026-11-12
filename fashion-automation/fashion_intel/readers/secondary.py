"""Secondary data: news and developments on the brands or companies.

The file can be a .docx, .xlsx, .csv or .txt. Each paragraph, row or line is one item. An item is
scored from the words it uses (launches and funding push up, layoffs and probes push down), or
from an explicit impact if the sheet has an "impact" column (a % or a fraction). Items that name
a different fashion player are left out; items that name no player are market-wide and are kept.
The score is only a direction and a size. The weight decides how far it moves the output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

POSITIVE = ("launch", "expan", "funding", "raises", "raised", "record", "surge", "growth", "grew",
            "partnership", "partners", "acquir", "strong", "beat", "profit", "new store",
            "festive", "sale", "discount", "campaign", "ipo", "investment", "rally", "boost",
            "wins", "popular", "demand", "upgrade")
NEGATIVE = ("layoff", "lay off", "recall", "probe", "fine", "penalty", "shutdown", "shut down",
            "decline", "loss", "ban", "fraud", "lawsuit", "slump", "outage", "delay", "closure",
            "closes", "resign", "exit", "cut", "weak", "downgrade", "default", "strike",
            "complaint", "raid", "crackdown", "slowdown", "drop", "falls", "fell")


@dataclass
class SecondaryItem:
    text: str
    score: float = 0.0               # -1 .. +1 from the wording
    impact: float | None = None      # explicit, as a fraction (0.03 = +3%)
    general: bool = False            # names no player: applies to the market


@dataclass
class SecondaryData:
    path: str = ""
    items: list[SecondaryItem] = field(default_factory=list)
    skipped: int = 0                 # items about other players

    @property
    def usable(self) -> list[SecondaryItem]:
        return [i for i in self.items if i.impact is not None or i.score != 0]

    def direction(self) -> float:
        """Net signal as a fraction: the mean explicit impact, else the mean wording score
        scaled to the largest effect (applied by the estimator)."""
        ex = [i.impact for i in self.items if i.impact is not None]
        if ex:
            return sum(ex) / len(ex)
        sc = [i.score for i in self.items if i.score != 0]
        return sum(sc) / len(sc) if sc else 0.0

    @property
    def explicit(self) -> bool:
        return any(i.impact is not None for i in self.items)


def score_text(text: str) -> float:
    low = text.lower()
    pos = sum(1 for w in POSITIVE if w in low)
    neg = sum(1 for w in NEGATIVE if w in low)
    return (pos - neg) / (pos + neg) if pos + neg else 0.0


def _lines(path: Path) -> tuple[list[str], list[float | None]]:
    ext = path.suffix.lower()
    if ext == ".docx":
        import docx
        d = docx.Document(str(path))
        out = [p.text.strip() for p in d.paragraphs]
        for t in d.tables:
            out += [" | ".join(c.text.strip() for c in r.cells) for r in t.rows]
        return [x for x in out if x], [None] * len(out)
    if ext in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        lines, impacts = [], []
        for ws in wb.worksheets:
            imp_col = None
            for r_i, row in enumerate(ws.iter_rows(values_only=True)):
                if r_i == 0:
                    for c_i, h in enumerate(row):
                        if h is not None and "impact" in str(h).lower():
                            imp_col = c_i
                    if imp_col is not None:
                        continue
                text = " ".join(str(c) for i, c in enumerate(row)
                                if c is not None and i != imp_col and not isinstance(c, (int, float)))
                imp = None
                if imp_col is not None and imp_col < len(row) and isinstance(row[imp_col], (int, float)):
                    v = float(row[imp_col])
                    imp = v / 100.0 if abs(v) > 1 else v
                if text.strip():
                    lines.append(text.strip())
                    impacts.append(imp)
        wb.close()
        return lines, impacts
    raw = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    out = [x.strip() for x in raw if x.strip()]
    return out, [None] * len(out)


def read_secondary(path: str | Path, own_terms: list[str],
                   other_terms: list[str]) -> SecondaryData:
    p = Path(path)
    lines, impacts = _lines(p)
    data = SecondaryData(path=str(p))
    for text, imp in zip(lines, impacts):
        if len(text) < 15 and imp is None:
            continue
        low = text.lower()
        mine = any(t in low for t in own_terms)
        theirs = any(t in low for t in other_terms)
        if theirs and not mine:
            data.skipped += 1
            continue
        data.items.append(SecondaryItem(text[:300], score_text(text), imp,
                                        general=not mine))
    return data
