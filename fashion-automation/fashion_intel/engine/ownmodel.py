"""The player sheet's own calculation chain, applied to the new month (Tata CLiQ, Snapdeal).

These two sheets are built as formulas: category gross GMV and cancellation give shipped GMV, shipped GMV and
ASP give items, a parent category is the sum of its parts, a parent's ASP is its GMV over its units, and the
sheet carries "Check" rows that must be zero. Each category row is first estimated on its own from history,
so they do not add up. This reads the sheet's formulas for its latest month and applies them to the new month:

* a derived row is recomputed from its inputs (shipped = gross x (1 - cancellation), items = GMV x 10 / ASP ...);
* where a Check row says a parent GMV equals the sum of its parts, the estimated parts keep their relative
  sizes and are scaled so they add up to the parent;
* where a Check row says one ASP equals another, the input row takes the derived one;
* where a Check row says category percentages weighted by shipped GMV equal the platform percentage,
  the percentages are scaled until they do.

A formula is used only if every cell it refers to is in the same month column and it uses nothing but
arithmetic, SUM and SUMPRODUCT.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from openpyxl.utils import get_column_letter

_CELL = re.compile(r"(?<![A-Za-z0-9_])\$?([A-Z]{1,3})\$?(\d+)(?![\d(])")
_RANGE = re.compile(r"\$?([A-Z]{1,3})\$?(\d+):\$?([A-Z]{1,3})\$?(\d+)")
_QA = re.compile(r"check|deviation|absolute|checklist", re.I)
_SKIP = re.compile(r"reported", re.I)
_FUNCS = {"SUM(", "SUMPRODUCT("}
FIRST_MODEL_ROW = 28            # the rows above are the platform topline, which the engine already derives


class Missing(Exception):
    pass


@dataclass
class Rel:
    row: int
    formula: str
    code: object
    deps: list[int]


@dataclass
class Model:
    month: str
    col: str
    rels: dict[int, Rel] = field(default_factory=dict)          # derived rows
    groups: list[tuple[int, list[int]]] = field(default_factory=list)     # (parent row, parts): parent = sum of parts
    weighted: list[tuple[list[int], list[int], int, int]] = field(default_factory=list)   # SUMPRODUCT(P, W) = T x U
    checks: dict[int, Rel] = field(default_factory=dict)        # the sheet's own check / deviation rows


def _translate(fm: str, col: str):
    f = fm[1:] if fm.startswith("=") else fm
    if "!" in f or '"' in f or any(ch in f for ch in "<>=") or "IF(" in f.upper():
        return None
    if {x for x in re.findall(r"[A-Z]{2,}\(", f)} - _FUNCS:
        return None
    ok = [True]
    deps: list[int] = []

    def rng(m):
        c1, r1, c2, r2 = m.groups()
        if c1 != col or c2 != col:
            ok[0] = False
            return "0"
        deps.extend(range(int(r1), int(r2) + 1))
        return f"R({r1},{r2})"

    f = _RANGE.sub(rng, f)

    def cell(m):
        c, r = m.groups()
        if c != col:
            ok[0] = False
            return "0"
        deps.append(int(r))
        return f"V({r})"

    f = _CELL.sub(cell, f).replace("^", "**").replace("SUMPRODUCT(", "SP(").replace("SUM(", "SM(")
    if not ok[0]:
        return None
    try:
        return compile(f, "<sheet formula>", "eval"), deps
    except SyntaxError:
        return None


def _rows_of(args: str, col: str) -> list[int] | None:
    """'DC266:DC267' or 'DC270,DC274:DC275' -> the rows, or None if any is outside the month column."""
    rows: list[int] = []
    for part in args.split(","):
        part = part.strip()
        m = re.fullmatch(r"\$?([A-Z]{1,3})\$?(\d+):\$?([A-Z]{1,3})\$?(\d+)", part)
        if m:
            if m.group(1) != col or m.group(3) != col:
                return None
            rows.extend(range(int(m.group(2)), int(m.group(4)) + 1))
            continue
        m = re.fullmatch(r"\$?([A-Z]{1,3})\$?(\d+)", part)
        if not m or m.group(1) != col:
            return None
        rows.append(int(m.group(2)))
    return rows


def _formulas(ref):
    """(month, column letter, {row: formula}) from the uploaded workbook, else from the copy kept in the config."""
    per_month = {m: sum(1 for r in ref.formulas if m in ref.formulas[r]) for m in ref.months}
    if per_month and max(per_month.values()) >= 20:
        top = max(per_month.values())
        month = next(m for m in reversed(ref.months) if per_month[m] >= 0.8 * top)
        return month, get_column_letter(ref.month_cols[month]), {r: f[month] for r, f in ref.formulas.items() if month in f}
    import json
    from pathlib import Path
    key = re.sub(r"[^a-z0-9]+", "", str(ref.player).lower())
    path = Path(__file__).resolve().parent.parent / "config" / "formulas" / f"{key}.json"
    if not path.exists():
        return None
    d = json.loads(path.read_text(encoding="utf-8"))
    return d["month"], d["col"], {int(r): f for r, f in d["formulas"].items()}


def _header_text(ref, row: int) -> str:
    """The heading of the block a row sits in: the nearest row above it that names a unit, e.g. 'Category ASP (INR)'."""
    for k in range(row - 1, 0, -1):
        n = str(ref.row_names.get(k) or "").strip()
        if not n or k in ref.row_to_par or _QA.search(n) or n.lower() == "total":
            continue
        if re.search(r"\([^)]{1,12}\)\s*$", n) or len(ref.series.get(k, {})) <= 4:
            return n.lower()
    return ""


def build(ref) -> Model | None:
    cached = getattr(ref, "_own_model", False)
    if cached is not False:
        return cached
    got = _formulas(ref)
    if got is None:
        ref._own_model = None
        return None
    month, col, fmap = got
    model = Model(month=month, col=col)
    c = rf"\$?{col}\$?"
    for row, fm in sorted(fmap.items()):
        if not fm or row < FIRST_MODEL_ROW:
            continue
        name = str(ref.row_names.get(row, ""))
        if _SKIP.search(name):
            continue
        if _QA.search(name):
            # what a check row says about the numbers
            m = (re.fullmatch(rf"=SUM\(([^)]*)\)\s*[-=]\s*{c}(\d+)", fm) or None)
            m2 = re.fullmatch(rf"={c}(\d+)\s*-\s*SUM\(([^)]*)\)", fm)
            parent = kids = None
            if m:
                parent, kids = int(m.group(2)), _rows_of(m.group(1), col)
            elif m2:
                parent, kids = int(m2.group(1)), _rows_of(m2.group(2), col)
            if parent and kids and "inr cr" in _header_text(ref, parent):
                if (parent, kids) not in model.groups:
                    model.groups.append((parent, kids))
            w = re.fullmatch(rf"=SUMPRODUCT\(([^,]+),([^)]+)\)\s*-\s*\(?{c}(\d+)\*{c}(\d+)\)?", fm)
            if w:
                p, q = _rows_of(w.group(1), col), _rows_of(w.group(2), col)
                if p and q and len(p) == len(q):
                    model.weighted.append((p, q, int(w.group(3)), int(w.group(4))))
            t = _translate(fm, col)
            if t is not None:
                model.checks[row] = Rel(row, fm, t[0], t[1])
            continue
        t = _translate(fm, col)
        if t is not None:
            model.rels[row] = Rel(row, fm, t[0], t[1], ) if False else Rel(row, fm, t[0], t[1])
    # a parent with no check row: its children whose formula is "parent x share" still add up to it
    shares: dict[int, list[int]] = {}
    for row, fm in fmap.items():
        sm = re.fullmatch(rf"={c}(\d+)\*\$?([A-Z]{{1,3}})\$?(\d+)", fm or "")
        if sm and row >= FIRST_MODEL_ROW and not _QA.search(str(ref.row_names.get(row, ""))):
            m2 = re.fullmatch(rf"=\$?{col}\$(\d+)\*\$?([A-Z]{{1,3}})\$?\d+", fm)
            if m2 and m2.group(2) != col:
                shares.setdefault(int(m2.group(1)), []).append(row)
    have = {p for p, _ks in model.groups}
    for p, ks in shares.items():
        if p not in have and "inr cr" in _header_text(ref, p):
            model.groups.append((p, sorted(ks)))
    # "=X-Y" checks on two ASPs: the input row takes the derived one
    for row, rel in list(model.checks.items()):
        m = re.fullmatch(rf"={c}(\d+)\s*-\s*{c}(\d+)", rel.formula)
        if m:
            x, y = int(m.group(1)), int(m.group(2))
            if x in model.rels and y not in model.rels and "asp" in _header_text(ref, y):
                model.rels[y] = Rel(y, f"={col}{x}", compile(f"V({x})", "<alias>", "eval"), [x])
    ref._own_model = model
    return model


def _env(V):
    def v(r):
        x = V.get(int(r))
        if x is None:
            raise Missing(r)
        return x

    def rg(a, b):
        return [V.get(i) for i in range(int(a), int(b) + 1)]

    def sm(*args):
        tot = 0.0
        for a in args:
            for x in (a if isinstance(a, list) else [a]):
                if x is not None:
                    tot += x
        return tot

    def sp(a, b):
        return sum(x * y for x, y in zip(a, b) if x is not None and y is not None)

    return {"V": v, "R": rg, "SM": sm, "SP": sp}


def evaluate(rel: Rel, V) -> float | None:
    try:
        x = eval(rel.code, {"__builtins__": {}}, _env(V))
    except (Missing, ZeroDivisionError, TypeError, ValueError, OverflowError):
        return None
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def block(ref, header_prefix: str, after: int = 0):
    """(header row, [member rows]) of a labelled block: the rows up to the next 'Total' or blank gap."""
    names = {r: str(n).strip().lower() for r, n in ref.row_names.items() if n}
    h = next((r for r, n in sorted(names.items()) if r > after and n.startswith(header_prefix)), None)
    if h is None:
        return None, []
    rows = []
    for r in range(h + 1, h + 14):
        n = names.get(r)
        if n is None or n == "total" or _QA.search(n):
            break
        rows.append(r)
    return h, rows


def _leaves(row: int, model: Model, seen=None) -> list[int]:
    """The input rows a row is made of: a row that is a plain SUM of others is made of theirs."""
    rel = model.rels.get(row)
    if rel is not None and re.fullmatch(r"=SUM\([^)]*\)", rel.formula):
        out: list[int] = []
        for d in rel.deps:
            out.extend(_leaves(d, model))
        return out
    return [] if rel is not None else [row]


def apply(eng, tol: float = 1e-9) -> int:
    """Recompute the derived rows for the new month. Returns how many rows were set."""
    from ..domain import Confidence, Provenance, Value
    ref = eng.ref
    model = build(ref)
    if model is None:
        return 0
    out = eng.out
    V: dict[int, float] = {}
    for r in ref.row_names:
        cur = out.get(r)
        if cur is not None and not cur.is_null and cur.value is not None:
            V[r] = cur.value
    V0 = dict(V)
    touched: set[int] = set()

    # the category gross GMVs add up to the platform's gross GMV; the category cancellations are set so that
    # the category shipped GMVs add up to the platform's shipped GMV
    gross, shipped = out.num(eng.P["gross_gmv"]), out.num(eng.P["shipped_gmv"])
    hg, grows = block(ref, "category gross gmv")
    hc, crows = block(ref, "category cancellation")
    if gross and shipped and grows and crows and len(grows) == len(crows):
        g = {r: V.get(r) for r in grows}
        cn = {r: V.get(cr) for r, cr in zip(grows, crows)}
        live = [r for r in grows if g[r] and cn[r] is not None]
        tot = sum(g[r] for r in live)
        if tot > 0 and 0.7 <= gross / tot <= 1.3:
            kg = gross / tot
            for r in live:
                V[r] = g[r] * kg
                touched.add(r)
            ship = sum(V[r] * (1 - cn[r]) for r in live)
            if ship > 0 and 0.7 <= shipped / ship <= 1.3:
                ks = shipped / ship
                for r, cr in zip(grows, crows):
                    if r in live:
                        V[cr] = min(max(1 - (1 - cn[r]) * ks, 0.0), 0.9)
                        touched.add(cr)

    def settle() -> bool:
        moved = False
        for _ in range(40):
            step = False
            for row, rel in model.rels.items():
                cur = out.get(row)
                if cur is not None and cur.provenance.is_actual:
                    continue
                x = evaluate(rel, V)
                if x is None or x < -1e-12:           # a GMV, units, ASP or share is never negative
                    continue
                if row not in V or abs(x - V[row]) > tol * max(1.0, abs(x)):
                    V[row] = x
                    touched.add(row)
                    step = moved = True
            if not step:
                break
        return moved

    depth = {}
    kids_of = {p: ks for p, ks in model.groups}
    children = {k for _p, ks in model.groups for k in ks}
    for p, _ks in model.groups:
        d, q = 0, p
        while q in children and d < 10:
            d += 1
            q = next((pp for pp, ks in model.groups if q in ks), None)
            if q is None:
                break
        depth[p] = d
    for _ in range(8):
        settle()
        changed = False
        for p, ks in sorted(model.groups, key=lambda g: depth.get(g[0], 0)):
            if p not in V or any(k not in V for k in ks):
                continue
            s = sum(V[k] for k in ks)
            if s <= 0 or V[p] <= 0 or abs(s / V[p] - 1) < 1e-9:
                continue
            f = V[p] / s
            if not 0.5 <= f <= 2.0:
                continue
            for k in ks:
                for leaf in _leaves(k, model):
                    if leaf in V:
                        V[leaf] *= f
                        touched.add(leaf)
                        changed = True
        if not changed:
            break
    settle()

    # category percentages weighted by their shipped GMV equal the platform's percentage
    in_p = {p for pr, _wr, _t, _u in model.weighted for p in pr}
    ordered = sorted(model.weighted, key=lambda w: w[2] in in_p)      # a check whose target is another's input goes last
    for _ in range(4):
        for pr, wr, t, u in ordered:
            if t in V and u in V:
                pairs = [(p, w) for p, w in zip(pr, wr) if p in V and w in V and p not in model.rels]
                cur = sum(V[p] * V[w] for p, w in pairs)
                if pairs and cur > 0:
                    f = V[t] * V[u] / cur
                    if abs(f - 1) > 1e-9 and 0.5 <= f <= 2.0:
                        for p, _w in pairs:
                            V[p] = min(V[p] * f, 1.0)
                            touched.add(p)

    from .itemblocks import item_blocks
    blk = item_blocks(ref)
    scope = {ref.par_to_row[t.par_id] for t in ref.template if t.par_id in ref.par_to_row}
    if blk:
        scope |= {e[k] for e in blk["entries"] for k in ("grow", "arow", "irow")} | {blk["total"]["row"]}
    n = 0
    for r in sorted(touched):
        if r not in scope or r not in V:
            continue
        cur = out.get(r)
        if cur is not None and cur.provenance.is_actual:
            continue
        rel = model.rels.get(r)
        how = (f"The sheet's own formula for this row ({rel.formula}), applied to this month's figures" if rel else
               "Estimated figure scaled so the parts add up to their parent, as the sheet's check requires")
        out.set(r, Value(V[r], Provenance.DERIVED, (cur.unit if cur is not None and cur.unit else ref.row_units.get(r, "")),
                         source="The sheet's own category model", method=how, confidence=Confidence.MEDIUM,
                         uncalibrated=V0.get(r)))
        n += 1
    return n


def check_rows(eng) -> list[tuple[int, str, float]]:
    """The sheet's own check / deviation rows evaluated on the new month: (row, label, value). All should be ~0."""
    ref = eng.ref
    model = build(ref)
    if model is None:
        return []
    V = {r: eng.out.num(r) for r in ref.row_names if eng.out.num(r) is not None}
    res = []
    for row, rel in sorted(model.checks.items()):
        x = evaluate(rel, V)
        if x is not None:
            res.append((row, str(ref.row_names.get(row, "")), x))
    return res
