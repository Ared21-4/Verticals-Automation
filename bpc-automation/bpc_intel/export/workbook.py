"""Excel export.

Two artefacts:

* The **output workbook** — the player's upload sheet for the reporting month,
  in exactly the reference template's five columns and row order, with
  unavailable parameters written as "null" as the template's own IFERROR does.
  Provenance sits on its own sheet so the deliverable stays clean.
* The **audit workbook** — inputs, validation, weights, call insights,
  discrepancies, calibration, adjustments and every produced value with its
  origin.

The reference workbook is never modified in place. A copy of the month's column
is offered separately so writing into the master model stays a conscious act.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from ..domain import MetricRows, Provenance, Severity, month_label, shift_months

# ----------------------------------------------------------------- house style

INK = "1F3864"
MUTED = "7F7F7F"
RULE = "D6DCE4"
BAND = "F2F5F9"

H1 = Font(name="Calibri", size=14, bold=True, color=INK)
H2 = Font(name="Calibri", size=10, color=MUTED, italic=True)
TH = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
TD = Font(name="Calibri", size=10)
TD_B = Font(name="Calibri", size=10, bold=True)
TD_MUTED = Font(name="Calibri", size=10, color=MUTED, italic=True)
GROUP = Font(name="Calibri", size=10, bold=True, color=INK)

FILL_HEAD = PatternFill("solid", fgColor=INK)
FILL_GROUP = PatternFill("solid", fgColor="E8EDF5")
FILL_BAND = PatternFill("solid", fgColor=BAND)

_HAIR = Side(style="thin", color=RULE)
BOX = Border(bottom=_HAIR)

DOT = {
    Provenance.REPORTED:    ("Reported",    "1E7B34"),
    Provenance.CALL_STATED: ("Call",        "0B6E99"),
    Provenance.DERIVED:     ("Derived",     "2F5597"),
    Provenance.ESTIMATED:   ("Estimated",   "B45309"),
    Provenance.CALIBRATED:  ("Calibrated",  "7A3E9D"),
    Provenance.ASSUMED:     ("Assumed",     "A33A3A"),
    Provenance.UNAVAILABLE: ("—",           MUTED),
}
SEV_COLOR = {Severity.ERROR: "B00020", Severity.WARNING: "B45309", Severity.INFO: "1E7B34"}


def _sheet(wb: Workbook, name: str, title: str, sub: str = "") -> tuple[Worksheet, int]:
    ws = wb.create_sheet(name[:31])
    ws.sheet_view.showGridLines = False
    ws.cell(1, 1, title).font = H1
    row = 3
    if sub:
        c = ws.cell(2, 1, sub)
        c.font = H2
        c.alignment = Alignment(wrap_text=False)
        row = 4
    return ws, row


def _head(ws: Worksheet, row: int, cols: list[tuple[str, int]],
          aligns: dict[int, str] | None = None) -> int:
    aligns = aligns or {}
    for i, (label, width) in enumerate(cols, start=1):
        c = ws.cell(row, i, label)
        c.font, c.fill = TH, FILL_HEAD
        c.alignment = Alignment(vertical="center",
                                horizontal=aligns.get(i, "left"))
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[row].height = 20
    ws.freeze_panes = ws.cell(row + 1, 1)
    return row + 1


def _row(ws: Worksheet, row: int, values: list[Any], *, band: bool = False,
         aligns: dict[int, str] | None = None, formats: dict[int, str] | None = None,
         fonts: dict[int, Font] | None = None, colors: dict[int, str] | None = None) -> None:
    aligns, formats, fonts, colors = aligns or {}, formats or {}, fonts or {}, colors or {}
    for i, v in enumerate(values, start=1):
        c = ws.cell(row, i, v)
        c.font = fonts.get(i, TD)
        if i in colors:
            c.font = Font(name="Calibri", size=10, color=colors[i],
                          bold=fonts.get(i, TD).bold)
        c.border = BOX
        if band:
            c.fill = FILL_BAND
        if i in aligns:
            c.alignment = Alignment(horizontal=aligns[i], vertical="top", wrap_text=False)
        if i in formats:
            c.number_format = formats[i]


def ordered_template(ref, est, cfg) -> list[tuple[Any, str]]:
    """Template rows in reading order: topline, categories, then the rest, nulls last.

    The par_id set and the five columns are unchanged, so the sheet still loads
    by par_id; only the row order differs from the reference template.
    """
    import re

    layout = cfg.get("output_layout") or {}
    sections = [s for s in layout.get("sections", []) if not s.get("_comment")]
    if not sections:
        return [(t, "") for t in ref.template]

    pats = [(s["name"], [re.compile(p) for p in s["match"]]) for s in sections]

    def bucket(q: str) -> int:
        for i, (_, ps) in enumerate(pats):
            if any(p.search(q or "") for p in ps):
                return i
        return len(pats) - 1

    def has_value(t) -> bool:
        v = est.value_for_par(ref, t.par_id)
        return v is not None and not v.is_null

    # Group by parent_question so a question's rows never split across sections,
    # keeping first-seen order within each section.
    groups: dict[str, list] = {}
    for t in ref.template:
        groups.setdefault(t.parent_question or "", []).append(t)

    out: list[tuple[Any, str]] = []
    tail: list[tuple[Any, str]] = []
    nulls_last = layout.get("nulls_last", True)

    for idx in range(len(pats)):
        for q, rows in groups.items():
            if bucket(q) != idx:
                continue
            name = pats[idx][0]
            filled = [r for r in rows if has_value(r)] if nulls_last else rows
            empty = [r for r in rows if not has_value(r)] if nulls_last else []
            out.extend((r, name) for r in filled)
            tail.extend((r, "Not available this month") for r in empty)

    return out + tail


# Headline figures every analysis opens with, then the category blocks.
KEY_METRICS = ("gross_gmv", "shipped_gmv", "net_gmv", "returned_gmv", "aov", "asp", "cancellation_pct",
               "returns_pct", "rto_pct", "rvp_pct", "mtu", "gross_transactions")
_KEY_BLOCKS = ("^category\\s*-?\\s*shipped\\s+gmv",              # category GMV
               "^category\\s*-?\\s*shipped\\s*%\\s*gmv",         # category % of shipped GMV
               "^category\\s*-?\\s*asp")                          # category ASP


def derived_history(ref, cfg) -> dict[int, dict[str, float]]:
    """Category shipped GMV for past months, where the player's sheet stores shares and not GMV.

    The engine derives category GMV as category share x platform shipped GMV for the month it runs.
    The same identity gives every earlier month, so the Analysis and History sheets show the full
    series instead of one column. A category GMV the sheet does hold is never overridden, and a
    total is the sum of the categories beneath it (those that exist that month), as in the engine.
    """
    cached = getattr(ref, "_derived_history", None)
    if cached is not None:
        return cached
    out: dict[int, dict[str, float]] = {}

    def series(par) -> dict[str, float]:
        row = ref.par_to_row.get(par) if par is not None else None
        if not row:
            return {}
        return {m: v for m in ref.months if (v := ref.value(row, m)) is not None}

    mt = cfg.get("metrics") or {}
    shipped = series(mt.get("shipped_gmv"))
    if not shipped:
        gross, canc = series(mt.get("gross_gmv")), series(mt.get("cancellation_pct"))
        shipped = {m: g * (1 - canc[m]) for m, g in gross.items() if m in canc}
    tree = cfg.get("fashion_tree") or cfg.get("category_tree") or {}
    pairs = {lf["gmv"]: lf["share"] for lf in tree.get("leaves", []) if lf.get("gmv") and lf.get("share")}
    for c in ((cfg.get("mis_generic") or {}).get("categories") or {}).values():
        if isinstance(c, dict) and c.get("gmv") and c.get("share"):
            pairs.setdefault(c["gmv"], c["share"])
    if shipped:
        for gpar, spar in pairs.items():
            if series(gpar):                       # the sheet holds this category's GMV itself
                continue
            sh = series(spar)
            d = {m: s * shipped[m] for m, s in sh.items() if m in shipped}
            if d:
                out[gpar] = d
    have = lambda p: out.get(p) or series(p)
    pending = {int(k): v for k, v in (tree.get("rollups") or {}).items()}
    while pending:
        progressed = False
        for par, parts in list(pending.items()):
            if any(p in pending for p in parts):
                continue
            del pending[par]
            progressed = True
            if series(par):
                continue
            months = set().union(*[set(have(p)) for p in parts])
            d = {m: sum(have(p)[m] for p in parts if m in have(p)) for m in months}
            if d:
                out[par] = d
        if not progressed:
            break
    # ---- rows the sheet does not carry: transactions and frequency follow from GMV, AOV and the users
    unit_of = {t.par_id: (t.unit or "") for t in ref.template}
    g_s, a_s, c_s = have(mt.get("gross_gmv")), have(mt.get("aov")), have(mt.get("cancellation_pct"))
    tp, fp, mp = mt.get("gross_transactions"), mt.get("frequency"), mt.get("mtu")
    if tp is not None and not series(tp) and g_s and a_s and unit_of.get(tp) == "Mn":
        d = {m: g * 10 / a_s[m] for m, g in g_s.items() if a_s.get(m)}
        if d:
            out[tp] = d
    t_s, m_s = have(tp), have(mp)
    if fp is not None and not series(fp) and t_s and m_s and unit_of.get(tp) == unit_of.get(mp):
        d = {m: t / m_s[m] for m, t in t_s.items() if m_s.get(m)}
        if d:
            out[fp] = d
    if mp is not None and not series(mp) and t_s and have(fp) and unit_of.get(mp) == unit_of.get(tp):
        d = {m: t / have(fp)[m] for m, t in t_s.items() if have(fp).get(m)}
        if d:
            out[mp] = d
    for tgt, src in (cfg.get("mirrors") or {}).items():          # a mirror row repeats its source in every month
        if str(tgt).isdigit() and not series(int(tgt)) and have(int(src)):
            out[int(tgt)] = dict(have(int(src)))
    ref._derived_history = out
    return out


def _hist_val(ref, row, derived: dict[str, float], m: str):
    """A past month's figure: the sheet's own, else the one derived from shares."""
    v = ref.value(row, m) if row else None
    return v if v is not None else derived.get(m)


UNIT_BASE, SHARE_BASE = 700000, 710000      # synthetic par_ids for the sheet's untagged category-units rows


def unit_layout(ref):
    """The sheet's "Category - Number of items shipped per month" block (Mn) and "As % total items sold".

    These rows carry no par_id, so each gets one from its sheet row. A leaf category's units are its share of
    shipped GMV x shipped GMV x 10 / its ASP (as the sheet computes them); a group row is the sum of the rows
    under it (read off the sheet's own figures); the total is the sum of the top-level rows."""
    cached = getattr(ref, "_unit_layout", False)
    if cached is not False:
        return cached
    names = ref.row_names

    def head(prefix, tagged_ok=False):
        return next((r for r, n in sorted(names.items())
                     if (tagged_ok or ref.row_to_par.get(r) is None) and str(n).strip().lower().startswith(prefix)), None)

    h_sh, h_it, h_is, h_asp = head("as % total shipped"), head("category - number of items"), \
        head("as % total items"), head("category - asp", True)
    if h_it and h_is and h_is > h_it + 1 and not h_asp:                 # the blocks are evenly spaced
        h_asp = h_is + (h_is - h_it)
    out = None
    if all((h_sh, h_it, h_is, h_asp)) and h_is > h_it + 1:
        ents = []
        grp = ""
        for k in range(1, h_is - h_it):
            sp, ap = ref.row_to_par.get(h_sh + k), ref.row_to_par.get(h_asp + k)
            label = str(names.get(h_it + k) or "").strip()
            leaf = bool(sp and ap)
            if not leaf:
                grp = label
            elif label in ("Women", "Men") and grp:
                label = f"{grp} - {label}"
            ents.append({"row": h_it + k, "par": UNIT_BASE + h_it + k, "irow": h_is + k, "ipar": SHARE_BASE + h_is + k,
                         "share": sp, "asp": ap, "label": label, "leaf": leaf, "kids": []})
        for i, e in enumerate(ents):
            if e["leaf"]:
                continue
            j, kids = i + 1, []
            while j < len(ents) and ents[j]["leaf"]:
                kids.append(j)
                j += 1
            # walk the rows below it, adding each, until the running sum equals the group's own figure in the sheet
            for m in reversed(ref.months):
                gv = ref.value(e["row"], m)
                if not gv or gv <= 0:
                    continue
                run, got, hit = 0.0, [], False
                for x in range(i + 1, len(ents)):
                    v = ref.value(ents[x]["row"], m)
                    if v is None:
                        break
                    run += v
                    got.append(x)
                    if abs(run - gv) <= 0.005 * gv:
                        hit = True
                        break
                    if run > gv * 1.005:
                        break
                if hit:
                    kids = got
                break
            e["kids"] = kids
        child = {x for e in ents for x in e["kids"]}
        for i, e in enumerate(ents):
            e["top"] = i not in child
            e["kids"] = [ents[x]["par"] for x in e["kids"]]
        out = {"entries": ents, "total": {"row": h_it, "par": UNIT_BASE + h_it,
                                          "label": "Total items shipped"}}
    ref._unit_layout = out
    return out


def layout_values(lay, get, shipped):
    """{par: figure} for the layout, from `get(par)` (a share or ASP figure) and shipped GMV."""
    vals: dict[int, float] = {}
    if not shipped:
        return vals
    for e in lay["entries"]:
        if e["leaf"]:
            s, a = get(e["share"]), get(e["asp"])
            if s is not None and a:
                vals[e["par"]] = s * shipped * 10 / a
    for e in reversed(lay["entries"]):   # a nested group is summed before the one it sits in
        if not e["leaf"]:
            mine = [vals[p] for p in e["kids"] if p in vals]
            if mine:
                vals[e["par"]] = sum(mine)
    top = [vals[e["par"]] for e in lay["entries"] if e["top"] and e["par"] in vals]
    if top and sum(top) > 0:
        tot = sum(top)
        vals[lay["total"]["par"]] = tot
        for e in lay["entries"]:
            if e["par"] in vals:
                vals[e["ipar"]] = vals[e["par"]] / tot
    return vals


def _unit_rows(ref, cfg, present: set[int], est=None) -> list[tuple[Any, str]]:
    """Units sold per category and segment (Mn): the engine models them (GMV x 10 / ASP) but they are not in
    the upload layout, so the Analysis adds them as their own block."""
    from ..readers.reference import ParameterRow

    out: list[tuple[Any, str]] = []

    def add(row, name):
        par = ref.row_to_par.get(row) if row else None
        if par and par not in present and all(r[0].par_id != par for r in out):
            out.append((ParameterRow("Units sold by category", name, int(par), "Mn", 0, added=True),
                        "Units by category"))

    for sk, sv in (cfg.get("segments") or {}).items():
        add(sv.get("items"), f"{ref.row_names.get(sv.get('items')) or sk} - total units")
    for lf in (cfg.get("l2_categories") or {}).values():
        add(lf.get("units"), f"{lf.get('label') or ref.row_names.get(lf.get('units'))} - units")
    lay = unit_layout(ref)
    if lay:
        if est is not None:
            from ..domain import Confidence, Value
            sh = est.value_for_par(ref, (cfg.get("metrics") or {}).get("shipped_gmv"))
            fig = lambda p: (lambda v: None if v is None or v.is_null else v.value)(est.value_for_par(ref, p))
            for par, x in layout_values(lay, fig, None if sh is None or sh.is_null else sh.value).items():
                if par not in est.by_par:
                    est.set_par(par, Value(x, Provenance.DERIVED, "%" if par >= SHARE_BASE else "Mn",
                                           source="Category share x shipped GMV x 10 / ASP",
                                           method="As the sheet: units = category share of shipped GMV x shipped "
                                                  "GMV x 10 / category ASP; groups and the total are sums.",
                                           confidence=Confidence.MEDIUM))

        def addp(par, row, name, u, q):
            if par not in present and all(r[0].par_id != par for r in out):
                ref.par_to_row.setdefault(par, row)
                out.append((ParameterRow(q, name, int(par), u, 0, added=True), "Units by category"))

        q1, q2 = "Category - Number of items shipped per month", "As % total items sold"
        addp(lay["total"]["par"], lay["total"]["row"], f"{lay['total']['label']} (Mn)", "Mn", q1)
        for e in lay["entries"]:
            addp(e["par"], e["row"], e["label"], "Mn", q1)
        for e in lay["entries"]:
            addp(e["ipar"], e["irow"], e["label"], "%", q2)
    return out


def analysis_template(ref, est, cfg) -> list[tuple[Any, str]]:
    """`ordered_template` for the analysis sheets: GMV, AOV, ASP and the other major metrics on top.

    The Output sheet keeps the portal's delivered order (it loads by par_id); the Analysis, History,
    MoM/YoY and deviation sheets read better with the headline figures first, so they use this.
    Each key row moves up, it is not copied, so every par_id still appears once.
    """
    import copy
    import re

    rows = ordered_template(ref, est, cfg)
    units = _unit_rows(ref, cfg, {t.par_id for t, _s in rows}, est)
    live = [(t, s) for t, s in rows if s != "Not available this month"]
    tail = [(t, s) for t, s in rows if s == "Not available this month"]
    by_par: dict[int, Any] = {}
    for t, _s in live:
        by_par.setdefault(t.par_id, t)
    key: list[Any] = []
    taken: set[int] = set()
    for name in KEY_METRICS:
        par = (cfg.get("metrics") or {}).get(name)
        if isinstance(par, int) and par in by_par and par not in taken:
            taken.add(par)
            head = copy.copy(by_par[par])          # the headline rows sit under "Key metrics", not their old parents
            head.parent_question = ""
            key.append(head)
    for pat in (re.compile(p, re.I) for p in _KEY_BLOCKS):
        for t, _s in live:
            q = (t.parent_question or "").strip()
            if pat.search(q) and "excluding" not in q.lower() and t.par_id not in taken:
                taken.add(t.par_id)
                key.append(t)
    if not key:
        return live + units + tail
    rest = [(t, s) for t, s in live if t.par_id not in taken]
    return [(t, "Key metrics") for t in key] + rest + units + tail


def _note(ws: Worksheet, row: int, text: str, width: int = 6) -> int:
    c = ws.cell(row, 1, text)
    c.font = TD_MUTED
    c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=width)
    ws.row_dimensions[row].height = max(15, 14 * (len(text) // (width * 18) + 1))
    return row + 1


# ================================================================== deliverable


def write_output_workbook(path: str | Path, ctx: dict[str, Any]) -> Path:
    ref, est, month, cfg = ctx["reference"], ctx["estimate"], ctx["month"], ctx["config"]
    label = month_label(month)

    wb = Workbook()
    wb.remove(wb.active)
    rows_in_order = ordered_template(ref, est, cfg)

    # ---- Sheet 1: the upload sheet, the template's five columns ------------
    ws = wb.create_sheet(cfg["reference"].get("output_sheet_name", "BPC-WF")[:31])
    ws.sheet_view.showGridLines = False
    hdr = _head(ws, 1, [("parent_question", 36), ("parameter_name", 42),
                        ("par_id", 9), ("unit", 10), (label, 15)],
                aligns={3: "center", 4: "center", 5: "right"})

    # One row per template parameter, no inserted banner rows: this is an upload
    # format, so the row count and columns stay fixed. Section breaks are shown
    # by shading alternate parent_question blocks, which costs no rows.
    r = hdr
    last_q = None
    last_section = None
    group_shaded = False
    pct = {"%"}
    for t, section in rows_in_order:
        if section != last_section:
            last_section, last_q = section, None
        first_of_group = bool(t.parent_question) and t.parent_question != last_q
        if first_of_group:
            group_shaded = not group_shaded
            last_q = t.parent_question

        v = est.value_for_par(ref, t.par_id)
        if v is None or v.is_null:
            out, fmt, font = "null", None, TD_MUTED
        else:
            out = round(v.value * 100 if t.unit in pct else v.value, 4)
            fmt = "0.00" if t.unit in pct else "#,##0.00"
            font = TD
        _row(ws, r,
             # The template leaves parent_question blank on continuation rows.
             [t.parent_question if first_of_group else "",
              t.parameter_name, t.par_id, t.unit, out],
             band=group_shaded,
             aligns={3: "center", 4: "center", 5: "right"},
             formats={5: fmt} if fmt else {},
             fonts={1: GROUP if first_of_group else TD, 5: font})
        if first_of_group:
            ws.cell(r, 1).alignment = Alignment(vertical="top")
        r += 1

    ws.auto_filter.ref = f"A{hdr - 1}:E{r - 1}"

    # ---- Contents: where each section starts -------------------------------
    ws0, r0 = _sheet(wb, "Contents", f"{cfg['display_name']} — {label}",
                     "Row order of the upload sheet. The par_id set and the five "
                     "columns are unchanged from the reference template.")
    r0 = _head(ws0, r0, [("section", 32), ("rows", 14), ("count", 10),
                         ("what it holds", 70)], aligns={2: "center", 3: "center"})
    start, prev, n = hdr, None, 0
    spans: list[tuple[str, int, int]] = []
    for i, (_, section) in enumerate(rows_in_order):
        if prev is not None and section != prev:
            spans.append((prev, start, hdr + i - 1))
            start = hdr + i
        prev = section
    if prev is not None:
        spans.append((prev, start, hdr + len(rows_in_order) - 1))
    blurb = {
        "Platform topline": "Platform GMV, AOV, ASP and the topline user metrics",
        "Upload sheet": "The 178 parameters of the BPC upload sheet, in its delivered order",
        "Additional metrics": "Shipped and net GMV, returns, ASP, basket size and category "
                              "shipped GMV: in the model, not in the upload sheet",
        "Other calculated metrics": "Everything else derived from the above",
        "Not available this month": "No evidence for these; exported as null",
    }
    for i, (name, a, b) in enumerate(spans):
        _row(ws0, r0, [name, f"{a}–{b}", b - a + 1, blurb.get(name, "")],
             band=bool(i % 2), aligns={2: "center", 3: "center"}, fonts={1: TD_B})
        r0 += 1

    added = [t for t, _ in rows_in_order if getattr(t, "added", False)]
    if added:
        r0 += 1
        ws0.cell(r0, 1, "Added beyond the reference template").font = GROUP
        r0 += 1
        r0 = _note(ws0, r0,
                   f"{len(added)} parameters are present in the model sheet but absent from "
                   "the BPC upload template, which carries category shares and ASPs but not "
                   "shipped GMV, net GMV, returns, platform ASP, basket size or category "
                   "shipped GMV. They are appended so those can be read from the sheet; the "
                   "template's own rows are unchanged. They are listed on the Source sheet as "
                   "'added by configuration'.", width=4)
        r0 = _head(ws0, r0, [("par_id", 9), ("parameter_name", 44)], aligns={1: "center"})
        for i, t in enumerate(added):
            _row(ws0, r0, [t.par_id, t.parameter_name], band=bool(i % 2),
                 aligns={1: "center"})
            r0 += 1

    # ---- Sheet 2: where each number came from -----------------------------
    ws2, r2 = _sheet(wb, "Source", f"{cfg['display_name']} — {label}: source of each value",
                     "Only 'Reported' values are actuals from the player MIS.")
    r2 = _head(ws2, r2, [("par_id", 9), ("parameter_name", 44), ("value", 14),
                         ("source type", 13), ("confidence", 11), ("in template", 12),
                         ("how it was produced", 86)],
               aligns={1: "center", 3: "right", 4: "center", 5: "center", 6: "center"})
    band = False
    for t, _section in rows_in_order:
        v = est.value_for_par(ref, t.par_id)
        if v is None or v.is_null:
            kind, color = DOT[Provenance.UNAVAILABLE]
            val, conf = "null", "—"
            how = (v.method if v is not None and v.method
                   else "no parameter in the model sheet for this par_id")
        else:
            kind, color = DOT[v.provenance]
            val = round(v.value * 100 if t.unit == "%" else v.value, 4)
            conf, how = v.confidence.value, v.method
        origin = "added by config" if getattr(t, "added", False) else "yes"
        _row(ws2, r2, [t.par_id, t.parameter_name, val, kind, conf, origin, how], band=band,
             aligns={1: "center", 3: "right", 4: "center", 5: "center", 6: "center"},
             formats={3: "#,##0.00"},
             colors={4: color, 6: ("B45309" if t.added else MUTED)},
             fonts={4: TD_B})
        band = not band
        r2 += 1

    _legend(wb)
    # Contents first, then the upload sheet.
    wb.move_sheet("Contents", offset=-(wb.sheetnames.index("Contents")))
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


def _legend(wb: Workbook) -> None:
    ws, r = _sheet(wb, "Key", "Key",
                   "Every number states how it came to exist.")
    r = _head(ws, r, [("source type", 16), ("meaning", 96)])
    rows = [
        (Provenance.REPORTED, "Stated in the player MIS for this month. An actual."),
        (Provenance.CALL_STATED, "Stated explicitly by the expert on the call."),
        (Provenance.DERIVED, "Arithmetic on other values, using the reference workbook's formula."),
        (Provenance.ESTIMATED, "Model estimate from history ending before this month."),
        (Provenance.CALIBRATED, "An estimate after a measured bias correction."),
        (Provenance.ASSUMED, "Carried forward. No evidence this month."),
        (Provenance.UNAVAILABLE, "No evidence. Written as null, never as zero."),
    ]
    for p, d in rows:
        name, color = DOT[p]
        _row(ws, r, [name, d], colors={1: color}, fonts={1: TD_B})
        r += 1


# ================================================================= audit pack


def write_audit_workbook(path: str | Path, ctx: dict[str, Any]) -> Path:
    ref, est, cal, wres = ctx["reference"], ctx["estimate"], ctx["calibration"], ctx["weights"]
    month, cfg = ctx["month"], ctx["config"]
    manifest, transcript = ctx.get("manifest", {}), ctx.get("transcript")
    findings = ctx.get("findings", []) + est.findings
    label = month_label(month)
    P = MetricRows(cfg.get("platform_rows") or {})

    wb = Workbook()
    wb.remove(wb.active)

    # ------------------------------------------------------------- Summary
    ws, r = _sheet(wb, "Summary", f"{cfg['display_name']} — {label}",
                   f"Generated {_dt.datetime.now():%d %b %Y %H:%M}  ·  "
                   f"run {manifest.get('run_id', 'n/a')}")

    ws.cell(r, 1, "Headline").font = GROUP
    r += 1
    r = _head(ws, r, [("metric", 26), ("value", 16), ("unit", 10),
                      ("source type", 13), ("vs prior month", 15), ("note", 90)],
              aligns={2: "right", 3: "center", 4: "center", 5: "right"})
    prior = shift_months(month, -1)
    for lab, key, unit in (("Shipped GMV", "shipped_gmv", "INR Cr"),
                           ("Gross GMV", "gross_gmv", "INR Cr"),
                           ("Net GMV", "net_gmv", "INR Cr"),
                           ("Cancellation", "cancellation_pct", "%"),
                           ("Returns", "returns_pct", "%"),
                           ("ASP", "asp", "INR"),
                           ("AOV", "aov", "INR"),
                           ("Basket size", "basket_size", "#"),
                           ("Gross transactions", "gross_transactions", "Mn"),
                           ("MTU", "mtu", "Mn")):
        v = est.rows.get(P[key])
        pv = ref.value(P[key], prior)
        if v is None or v.is_null:
            _row(ws, r, [lab, "unavailable", unit, "—", ""], aligns={2: "right"})
        else:
            kind, color = DOT[v.provenance]
            mom = (v.value / pv - 1) if pv else None
            note = next((n for n in v.notes if n.startswith("History only")), "")
            if v.confidence.value == "low" and key in ("gross_gmv", "shipped_gmv") and not note:
                note = "low confidence"
            _row(ws, r, [lab, v.value, unit, kind, mom, note],
                 aligns={2: "right", 3: "center", 4: "center", 5: "right"},
                 formats={2: "0.00%" if unit == "%" else "#,##0.00", 5: "+0.0%;-0.0%"},
                 colors={4: color}, fonts={1: TD_B, 4: TD_B})
        r += 1

    # Category GMV: the three BPC categories.
    tree_rows = [("Grooming (personal care)", 249), ("Makeup & Fragrance", 250), ("Others", 4571)]
    found = [(lab, est.value_for_par(ref, par)) for lab, par in tree_rows]
    if any(v is not None and not v.is_null for _, v in found):
        r += 1
        ws.cell(r, 1, "BPC category GMV").font = GROUP
        r += 1
        r = _head(ws, r, [("category", 26), ("value", 16), ("unit", 10),
                          ("source type", 13), ("share of shipped GMV", 15)],
                  aligns={2: "right", 3: "center", 4: "center", 5: "right"})
        ship = est.num(P["shipped_gmv"])
        for lab, v in found:
            if v is None or v.is_null:
                _row(ws, r, [lab, "unavailable", "INR Cr", "—", ""], aligns={2: "right"})
            else:
                kind, color = DOT[v.provenance]
                _row(ws, r, [lab, v.value, "INR Cr", kind,
                             (v.value / ship) if ship else None],
                     aligns={2: "right", 3: "center", 4: "center", 5: "right"},
                     formats={2: "#,##0.00", 5: "0.0%"}, colors={4: color},
                     fonts={1: TD_B, 4: TD_B})
            r += 1

    r += 1
    ws.cell(r, 1, "Run").font = GROUP
    r += 1
    r = _head(ws, r, [("item", 26), ("value", 70)])
    hist = len([m for m in ref.months if m < month])
    for k, v in (
            ("Player", cfg["display_name"]),
            ("Reporting month", label),
            ("Reference model sheet", ref.model_sheet),
            ("History used", f"{hist} months, {month_label(ref.months[0])} to "
                             f"{month_label(prior)}"),
            ("Run confidence", wres.confidence.value),
            ("Evidence weight available", f"{wres.available_configured_weight:.0%} of configured"),
            ("Errors", sum(1 for f in findings if f.severity is Severity.ERROR)),
            ("Warnings", sum(1 for f in findings if f.severity is Severity.WARNING)),
            ("Parameters produced", len([v for v in est.rows.values() if not v.is_null])),
            ("Parameters unavailable", len([v for v in est.rows.values() if v.is_null]))):
        _row(ws, r, [k, v], fonts={1: TD_B})
        r += 1

    # --------------------------------------------------------------- Trend
    from ..trend import reference_months, trend_rows
    rows_t = trend_rows(est, ref, cfg, month)
    ws, r = _sheet(wb, "Trend", f"Trend and deviation, {label}",
                   f"Against the reference's own history ({reference_months(ref)}). Percent "
                   "metrics are in percentage points. A flag means look again, not that the "
                   "figure is wrong: sales and festivals move GMV.")
    r = _head(ws, r, [("metric", 26), (label, 14), ("source type", 13), ("last month", 13),
                      ("vs last month", 14), ("same month last year", 20), ("vs last year", 13),
                      ("recent 3-mo average", 18), ("vs average", 12),
                      ("reference's figure for this month", 24), ("vs reference", 13),
                      ("last year, reconciled", 20), ("vs last year, reconciled", 22),
                      ("base used for the reconciliation", 46), ("flag", 40)],
              aligns={i: "right" for i in (2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13)} | {3: "center"})
    for i, t in enumerate(rows_t):
        kind, color = DOT[Provenance(t["source"])]
        chg = "+0.0%;-0.0%" if not t["pp"] else '+0.0"pp";-0.0"pp"'
        num = "0.00%" if t["pp"] else "#,##0.0"
        pp = lambda v: v * 100 if t["pp"] and v is not None else v
        _row(ws, r, [t["metric"], t["value"], kind, t["prior_month"], pp(t["mom"]),
                     t["year_ago"], pp(t["yoy"]), t["recent_avg"], pp(t["vs_recent_avg"]),
                     t["reference_figure"], pp(t["vs_reference"]),
                     t["year_ago_reconciled"], pp(t["yoy_reconciled"]), t["base_basis"],
                     t["flag"]],
             band=bool(i % 2),
             aligns={i2: "right" for i2 in (2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13)} | {3: "center"},
             formats={2: num, 4: num, 5: chg, 6: num, 7: chg, 8: num, 9: chg, 10: num, 11: chg,
                      12: num, 13: chg},
             colors={3: color, 15: "B45309"}, fonts={1: TD_B, 3: TD_B})
        r += 1
    r += 1
    for rc in getattr(est, "reconciliations", {}).values():
        if rc.diagnosis not in ("consistent", "insufficient"):
            r = _note(ws, r, f"{rc.metric}: {rc.narrative}", width=10)
    r = _note(ws, r, "The reference's figure for this month is whatever the reference workbook "
                     "already held there, typically an earlier estimate. Uploading the reference "
                     "with your latest numbers makes this comparison, and the trend columns, "
                     "reflect them.", width=8)

    # ------------------------------------------------------------- Balance
    from ..trend import balance_view
    bv = balance_view(est, ctx.get("mis_only"), ref, ctx.get("past_calibration") or [], month)
    ws, r = _sheet(wb, "Balance", "Output against the MIS, and the history balance",
                   "The MIS figure is kept beside the output. The output is moved toward the "
                   "level whose MoM and YoY, on the stored history, best match what the MIS "
                   "states, so neither is far off the other.")
    if bv["this"]:
        t0 = bv["this"]
        r = _head(ws, r, [("item", 44), ("value", 18), ("note", 80)], aligns={2: "right"})
        for k, v, f, n in (
                ("Gross GMV reported by the MIS", t0["mis_level"], "#,##0.0", ""),
                ("Level the stored history points to", t0["history_level"], "#,##0.0",
                 "Where MoM and YoY, on stored history, are jointly closest to the MIS's stated ones"),
                ("History weight applied", t0["weight"], "0%", t0["weight_basis"]),
                ("Gross GMV in the output", t0["output"], "#,##0.0",
                 "capped at the maximum adjustment" if t0["capped"] else ""),
                ("Output against the MIS", t0["deviation"], "+0.0%;-0.0%", ""),
                ("YoY the MIS states", t0["mis_yoy"], "+0.0%;-0.0%", ""),
                ("YoY on stored history: at the MIS level", t0["yoy_mis_level"], "+0.0%;-0.0%", ""),
                ("YoY on stored history: output", t0["yoy_output"], "+0.0%;-0.0%", ""),
                ("MoM the MIS states", t0["mis_mom"], "+0.0%;-0.0%", ""),
                ("MoM on stored history: at the MIS level", t0["mom_mis_level"], "+0.0%;-0.0%", ""),
                ("MoM on stored history: output", t0["mom_output"], "+0.0%;-0.0%", "")):
            _row(ws, r, [k, v, n], aligns={2: "right"}, formats={2: f}, fonts={1: TD_B})
            r += 1
        r += 1
        r = _note(ws, r, t0["narrative"], width=3)
    else:
        r = _note(ws, r, "No history balance was applied this run: the MIS stated no MoM or YoY, "
                         "its growth already agreed with the stored history, or the history weight "
                         "was 0.", width=3)
    if bv["earlier"]:
        r += 1
        ws.cell(r, 1, "Earlier saved months").font = GROUP
        r += 1
        r = _head(ws, r, [("month", 44), ("MIS level", 18), ("output", 18), ("deviation", 14),
                          ("history weight", 15), ("history sat from MIS", 20)],
                  aligns={2: "right", 3: "right", 4: "right", 5: "right", 6: "right"})
        for i, e_ in enumerate(bv["earlier"]):
            _row(ws, r, [month_label(e_["month"]), e_["mis_level"], e_["output"], e_["deviation"],
                         e_["weight"], e_["history_gap"]], band=bool(i % 2),
                 aligns={2: "right", 3: "right", 4: "right", 5: "right", 6: "right"},
                 formats={2: "#,##0.0", 3: "#,##0.0", 4: "+0.0%;-0.0%", 5: "0%", 6: "0.0%"})
            r += 1
        r += 1
        r = _note(ws, r, "History is saved as the MIS reports it, so the stored bases move toward "
                         "the MIS each month, and the weight given to history falls when it has "
                         "sat far from the MIS. Both narrow the gap between output and MIS.",
                  width=6)
    if bv["deviations"]:
        r += 1
        ws.cell(r, 1, "Output against the MIS-based figures, by metric").font = GROUP
        r += 1
        r = _head(ws, r, [("metric", 44), ("MIS-based", 18), ("output", 18), ("deviation", 14)],
                  aligns={2: "right", 3: "right", 4: "right"})
        for i, d in enumerate(bv["deviations"]):
            _row(ws, r, [d["metric"], d["mis_based"], d["output"], d["deviation"]],
                 band=bool(i % 2), aligns={2: "right", 3: "right", 4: "right"},
                 formats={2: "#,##0.0", 3: "#,##0.0", 4: "+0.0%;-0.0%"})
            r += 1

    # -------------------------------------------------------------- Inputs
    ws, r = _sheet(wb, "Inputs", "Input files and reporting periods")
    r = _head(ws, r, [("kind", 22), ("file", 50), ("period", 20),
                      ("status", 12), ("note", 56)], aligns={4: "center"})
    for i, f in enumerate(manifest.get("inputs", [])):
        _row(ws, r, [f.get("kind", ""), f.get("file", ""), f.get("period", ""),
                     f.get("status", ""), f.get("note", "")],
             band=bool(i % 2), aligns={4: "center"})
        r += 1

    # ---------------------------------------------------------- Validation
    ws, r = _sheet(wb, "Validation", "Validation and reconciliation",
                   "Errors must be resolved before the output is relied on. "
                   "Warnings are judgement calls.")
    r = _head(ws, r, [("severity", 11), ("metric", 24), ("finding", 66), ("detail", 90)],
              aligns={1: "center"})
    order = {"error": 0, "warning": 1, "info": 2}
    for i, f in enumerate(sorted(findings, key=lambda x: order[x.severity.value])):
        _row(ws, r, [f.severity.value.upper(), f.metric, f.message, f.detail],
             band=bool(i % 2), aligns={1: "center"},
             colors={1: SEV_COLOR[f.severity]}, fonts={1: TD_B})
        r += 1

    # ------------------------------------------------------------- Weights
    ws, r = _sheet(wb, "Weights", "Evidence weights",
                   "Configured is standing policy. Effective is what this run used after "
                   "removing missing sources and discounting weak ones.")
    r = _head(ws, r, [("source", 46), ("configured", 12), ("available", 11),
                      ("quality", 10), ("effective", 11), ("why discounted", 44)],
              aligns={2: "right", 3: "center", 4: "center", 5: "right"})
    for i, d in enumerate(wres.table()):
        _row(ws, r, [d["source"], d["configured_pct"] / 100, "yes" if d["available"] else "no",
                     d["quality_multiplier"], d["effective_pct"] / 100,
                     d["reasons"] or d["note"]],
             band=bool(i % 2), aligns={2: "right", 3: "center", 4: "center", 5: "right"},
             formats={2: "0.0%", 4: "0.00", 5: "0.0%"},
             colors={3: "1E7B34" if d["available"] else "B00020"})
        r += 1
    r += 1
    for w in wres.warnings:
        r = _note(ws, r, w)

    # ------------------------------------------------------- Call insights
    ws, r = _sheet(wb, "Call insights", "Extracted expert call insights",
                   "Evidence class separates a reported figure from an opinion, "
                   "an expectation or an inference.")
    r = _head(ws, r, [("section", 30), ("class", 16), ("category", 22),
                      ("value", 10), ("statement", 92), ("limitations", 44)],
              aligns={2: "center", 4: "right"})
    if transcript:
        for i, ins in enumerate(transcript.insights):
            val = (f"{ins.value:.0%}" if ins.value is not None and ins.unit == "%"
                   else (ins.value if ins.value is not None else ""))
            _row(ws, r, [ins.section, ins.kind, ins.category, val,
                         ins.statement, ins.limitations],
                 band=bool(i % 2), aligns={2: "center", 4: "right"})
            r += 1

    # ---------------------------------------------------- Call caveats
    ws, r = _sheet(wb, "Call caveats", "Transcript clarifications",
                   "Passages the transcript flagged rather than resolved by assumption.")
    r = _head(ws, r, [("section", 24), ("type", 22), ("text", 130)])
    if transcript:
        for i, c in enumerate(transcript.clarifications):
            _row(ws, r, [c["section"], c["type"], c["text"]], band=bool(i % 2))
            r += 1

    # ------------------------------------------------------------- Metrics
    ws, r = _sheet(wb, "Metrics", f"Every value produced for {label}",
                   "'Workbook before run' is the reference workbook's pre-existing figure, "
                   "left unchanged.")
    r = _head(ws, r, [("par_id", 8), ("metric", 42), ("unit", 9), (label, 14),
                      ("source type", 13), ("conf.", 9), ("prior month", 13),
                      ("MoM", 9), ("YoY", 9), ("workbook before run", 17),
                      ("how it was produced", 80)],
              aligns={1: "center", 3: "center", 4: "right", 5: "center", 6: "center",
                      7: "right", 8: "right", 9: "right", 10: "right"})
    yoy_m = shift_months(month, -12)
    i = 0
    for row_idx in sorted(est.rows):
        v = est.rows[row_idx]
        name = ref.row_names.get(row_idx, "")
        if not name:
            continue
        pv, yv = ref.value(row_idx, prior), ref.value(row_idx, yoy_m)
        kind, color = DOT[v.provenance]
        _row(ws, r, [ref.row_to_par.get(row_idx, ""), name, v.unit, v.value, kind,
                     v.confidence.value, pv,
                     (v.value / pv - 1) if v.value is not None and pv else None,
                     (v.value / yv - 1) if v.value is not None and yv else None,
                     ref.value(row_idx, month), v.method],
             band=bool(i % 2),
             aligns={1: "center", 3: "center", 4: "right", 5: "center", 6: "center",
                     7: "right", 8: "right", 9: "right", 10: "right"},
             formats={4: "#,##0.0000", 7: "#,##0.0000", 8: "+0.0%;-0.0%",
                      9: "+0.0%;-0.0%", 10: "#,##0.0000"},
             colors={5: color}, fonts={5: TD_B})
        r += 1
        i += 1

    # Values derived for parameters this player's sheet has no row for, such as
    # category GMV for a player that stores shares. They have no stored history.
    pnames = {t.par_id: t.parameter_name for t in ref.template}
    for par, v in sorted(est.by_par.items()):
        if par in ref.par_to_row or v is None or v.is_null:
            continue
        kind, color = DOT[v.provenance]
        _row(ws, r, [par, pnames.get(par, f"par {par}"), v.unit, v.value, kind,
                     v.confidence.value, None, None, None, None, v.method],
             band=bool(i % 2),
             aligns={1: "center", 3: "center", 4: "right", 5: "center", 6: "center"},
             formats={4: "#,##0.0000"}, colors={5: color}, fonts={5: TD_B})
        r += 1
        i += 1

    # ------------------------------------------------------- Corroboration
    cor = ctx.get("corroboration")
    ws, r = _sheet(wb, "Corroboration", "Independent cross-checks of the MIS",
                   "Each rebuilds a category from figures the expert stated separately. "
                   "They use no MIS input, so agreement tests the reported value.")
    if cor and cor.checks:
        r = _head(ws, r, [("check", 44), ("category", 22), ("implied by call", 16),
                          ("reported in MIS", 16), ("deviation", 12), ("verdict", 15),
                          ("built from", 68)],
                  aligns={3: "right", 4: "right", 5: "right", 6: "center"})
        for i, c in enumerate(cor.checks):
            d = c.to_dict()
            col = {"corroborated": "1E7B34", "consistent": "1E7B34",
                   "conflicting": "B00020"}[d["verdict"]]
            _row(ws, r, [d["check"], d["category"], d["expected_from_call"],
                         d["reported_in_mis"], d["deviation"], d["verdict"], d["inputs"]],
                 band=bool(i % 2),
                 aligns={3: "right", 4: "right", 5: "right", 6: "center"},
                 formats={3: "#,##0.00", 4: "#,##0.00", 5: "+0.00%;-0.00%"},
                 colors={5: col, 6: col}, fonts={5: TD_B, 6: TD_B})
            r += 1
        r += 1
    for s in (cor.statements if cor else ["No cross-check was possible."]):
        r = _note(ws, r, s, width=7)

    # --------------------------------------------------------- Calibration
    ws, r = _sheet(wb, "Calibration", "Historical calibration",
                   "What this month's actual and the expert's YoY together imply the "
                   "year-ago value must have been. Stored history was not changed.")
    if cal.implied:
        r = _head(ws, r, [("category", 26), (f"MIS {label}", 14), ("expert YoY", 12),
                          (f"implied {month_label(cal.prior_month)}", 18),
                          (f"stored {month_label(cal.prior_month)}", 18),
                          ("deviation", 12), ("direction", 14)],
                  aligns={2: "right", 3: "right", 4: "right", 5: "right",
                          6: "right", 7: "center"})
        for i, imp in enumerate(sorted(cal.implied, key=lambda x: -abs(x.error_pct))):
            d = imp.to_dict()
            sev = (SEV_COLOR[Severity.ERROR] if abs(d["error_pct"]) > 0.25
                   else SEV_COLOR[Severity.WARNING] if abs(d["error_pct"]) > 0.10
                   else SEV_COLOR[Severity.INFO])
            _row(ws, r, [d["category"], d["mis_current"], d["call_yoy"],
                         d["implied_prior"], d["stored_prior"], d["error_pct"],
                         d["direction"]],
                 band=bool(i % 2),
                 aligns={2: "right", 3: "right", 4: "right", 5: "right",
                         6: "right", 7: "center"},
                 formats={2: "#,##0.0", 3: "+0%;-0%", 4: "#,##0.0", 5: "#,##0.0",
                          6: "+0.0%;-0.0%"},
                 colors={6: sev}, fonts={6: TD_B})
            r += 1
        r += 1
        for k, v, f in (("Categories measured", cal.n, None),
                        ("Mean absolute deviation", cal.mape, "0.0%"),
                        ("Signed mean deviation", cal.mpe, "+0.0%;-0.0%"),
                        ("Conclusion", cal.systematic, None)):
            _row(ws, r, [k, v], aligns={2: "right"},
                 formats={2: f} if f else {}, fonts={1: TD_B})
            r += 1
    r += 1
    for s in cal.statements:
        r = _note(ws, r, s, width=7)

    # ----------------------------------------------------------- Backtests
    ws, r = _sheet(wb, "Backtests", "Walk-forward backtests",
                   "Each month is re-estimated from data before it only. "
                   "These are genuine out-of-sample errors.")
    r = _head(ws, r, [("metric", 30), ("months tested", 14), ("mean abs error", 15),
                      ("signed error", 14), ("direction correct", 17)],
              aligns={2: "center", 3: "right", 4: "right", 5: "right"})
    for i, b in enumerate(cal.backtests):
        _row(ws, r, [b.metric, b.n, b.mape, b.mpe, b.direction_accuracy],
             band=bool(i % 2), aligns={2: "center", 3: "right", 4: "right", 5: "right"},
             formats={3: "0.00%", 4: "+0.00%;-0.00%", 5: "0%"})
        r += 1
    if not cal.backtests:
        _note(ws, r, "No estimator had enough history for a walk-forward backtest. "
                     "No forecast accuracy is claimed.")

    # --------------------------------------------------------- Adjustments
    ws, r = _sheet(wb, "Adjustments", "Material adjustments",
                   "Each names the business driver it represents, so the same driver "
                   "cannot be applied twice through two levers.")
    r = _head(ws, r, [("metric", 30), ("business driver", 30), ("rationale", 110),
                      ("source", 30)])
    for i, a in enumerate(est.adjustments):
        d = a.to_dict()
        _row(ws, r, [d["metric"], d["driver"], d["rationale"], d["source"]],
             band=bool(i % 2))
        r += 1
    r += 1
    ws.cell(r, 1, "Drivers claimed").font = GROUP
    r += 1
    r = _head(ws, r, [("driver", 30), ("claimed by", 110)])
    for k, v in est.drivers_claimed.items():
        _row(ws, r, [k, v], fonts={1: TD_B})
        r += 1

    # -------------------------------------------------------- Method changes
    ws, r = _sheet(wb, "Method changes",
                   "Methodology changes against the reference workbook")
    r = _head(ws, r, [("metric", 24), ("reference workbook did", 52),
                      ("this run does", 60), ("why", 86)])
    for i, m in enumerate(est.method_changes):
        _row(ws, r, [m["metric"], m["was"], m["now"], m["why"]], band=bool(i % 2))
        r += 1
    if not est.method_changes:
        _note(ws, r, "No methodology changes in this run.", width=4)

    # ------------------------------------------------------------ Overrides
    ws, r = _sheet(wb, "Overrides", "Manual overrides",
                   "An override never rewrites source data. The original value and "
                   "the reason are kept.")
    r = _head(ws, r, [("metric", 34), ("original", 14), ("override", 14),
                      ("reason", 66), ("author", 18), ("at", 20)],
              aligns={2: "right", 3: "right"})
    for i, o in enumerate(ctx.get("overrides", [])):
        _row(ws, r, [o.get("metric"), o.get("original"), o.get("new_value"),
                     o.get("reason"), o.get("author"), o.get("at")],
             band=bool(i % 2), aligns={2: "right", 3: "right"},
             formats={2: "#,##0.0000", 3: "#,##0.0000"})
        r += 1
    if not ctx.get("overrides"):
        _note(ws, r, "No manual overrides in this run.", width=6)

    _legend(wb)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


# ============================================================== model column


def write_model_column(path: str | Path, ctx: dict[str, Any]) -> Path:
    ref, est, month, cfg = ctx["reference"], ctx["estimate"], ctx["month"], ctx["config"]
    label = month_label(month)
    wb = Workbook()
    wb.remove(wb.active)
    ws, r = _sheet(wb, "Model column", f"{ref.model_sheet} — {label}",
                   "Paste the value column into the reporting-month column of the model "
                   "sheet. The reference workbook was not modified.")
    r = _head(ws, r, [("model row", 11), ("par_id", 9), ("metric", 46),
                      ("unit", 9), (label, 16), ("source type", 13)],
              aligns={1: "center", 2: "center", 4: "center", 5: "right", 6: "center"})
    i = 0
    for row_idx in range(cfg["reference"]["first_data_row"],
                         cfg["reference"]["last_data_row"] + 1):
        v = est.rows.get(row_idx)
        name = ref.row_names.get(row_idx, "")
        if v is None and not name:
            continue
        kind, color = DOT[v.provenance] if v else ("—", MUTED)
        _row(ws, r, [row_idx, ref.row_to_par.get(row_idx, ""), name,
                     v.unit if v else "", v.value if v and not v.is_null else None, kind],
             band=bool(i % 2),
             aligns={1: "center", 2: "center", 4: "center", 5: "right", 6: "center"},
             formats={5: "#,##0.0000"}, colors={6: color})
        r += 1
        i += 1
    _legend(wb)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


# ================================================================== analysis

# Red for low or falling, yellow for the middle (zero, for growth), green for high or rising.
RYG_RED, RYG_YELLOW, RYG_GREEN = "F8696B", "FFEB84", "63BE7B"


def add_growth_colours(ws: Worksheet, rel_ranges: list[str], pp_ranges: list[str],
                       rel_span: float = 0.30, pp_span: float = 5.0) -> None:
    """Red for falls, green for rises, white at zero. Values are fractions (rel) or points (pp)."""
    from openpyxl.formatting.rule import ColorScaleRule
    for rngs, span in ((rel_ranges, rel_span), (pp_ranges, pp_span)):
        if rngs:
            ws.conditional_formatting.add(" ".join(rngs), ColorScaleRule(
                start_type="num", start_value=-span, start_color=RYG_RED,
                mid_type="num", mid_value=0, mid_color=RYG_YELLOW,
                end_type="num", end_value=span, end_color=RYG_GREEN))


# Splits the master workbooks close with "= 1 - SUM(the rest)": the last member present is the residual.
KNOWN_SPLITS = ([286, 287], [221, 222], [162, 163], [7201, 7202], [223, 224, 225, 226],
                [1409, 1410, 1411, 1412], [476, 477, 478], [1431, 1579], [446, 7601])


def _link_formulas(ws, arow: dict[int, int], col: str, curv: dict[int, Any], cfg: dict,
                   month: str | None = None, ref=None, keep_formulas: bool = False) -> int:
    """Make the current month's derived figures formulas, as the master workbooks define them.

    The masters (Fashion, BPC, Furniture, Babycare) compute these in the sheet itself, from the same month's
    inputs; AOV, category ASP and the shares roll forward from last month and stay inputs. So here:
    shipped = gross x (1 - cancellation); returns = RTO + RVP; returned = returns % x its base; net = shipped -
    returned; transactions = gross x 10^7 / AOV; MTU and frequency from transactions or MAU; category GMV =
    share x shipped, totals = sums, the residual category = 1 - the rest; units = GMV x 10^7 / ASP and their
    shares; platform ASP = GMV over units; each split's last member = 1 - the others; orders per day.
    A formula is written only where it reproduces the figure already in the cell, so an unedited workbook shows
    the same numbers; edit an input and everything downstream follows. Returns how many cells were linked.
    """
    import calendar

    mt = cfg.get("metrics") or {}
    P = lambda k: mt.get(k) if isinstance(mt.get(k), int) else None
    cell = lambda par: f"{col}{arow[par]}"
    val = lambda par: curv.get(par)
    linked: set[int] = set()

    def put(par, formula: str, expect, tol: float = 1e-6) -> bool:
        v = val(par) if par is not None else None
        if par is None or par not in arow or par in linked or v is None or expect is None:
            return False
        if abs(expect - v) > tol * max(1.0, abs(v)):
            return False
        if keep_formulas and isinstance(ws[cell(par)].value, str) and ws[cell(par)].value.startswith("="):
            return False
        ws[cell(par)].value = formula
        linked.add(par)
        return True

    have = lambda *ps: all(p is not None and p in arow and val(p) is not None for p in ps)
    G, C, S = P("gross_gmv"), P("cancellation_pct"), P("shipped_gmv")
    R, N, RET = P("returns_pct"), P("net_gmv"), P("returned_gmv")
    RTO, RVP, A, T = P("rto_pct"), P("rvp_pct"), P("aov"), P("gross_transactions")
    F, M, MAU, MAC, AS = P("frequency"), P("mtu"), P("mau"), P("mac"), P("asp")
    base = G if cfg.get("returns_base", "shipped") == "gross" else S
    fc = cfg.get("furniture") or cfg.get("babycare") or {}

    # ---- the GMV family
    if have(G, C, S):
        put(S, f"={cell(G)}*(1-{cell(C)})", val(G) * (1 - val(C)))
    if not cfg.get("returns_direct") and have(R, RTO, RVP):
        put(R, f"={cell(RTO)}+{cell(RVP)}", val(RTO) + val(RVP))
    if have(RET, R, base):
        put(RET, f"={cell(base)}*{cell(R)}", val(base) * val(R))
    if have(N, S):
        if have(RET):
            put(N, f"={cell(S)}-{cell(RET)}", val(S) - val(RET))
        elif have(R, base):
            put(N, f"={cell(S)}-{cell(base)}*{cell(R)}", val(S) - val(base) * val(R))

    # ---- transactions, users, frequency
    if have(T, G, A) and val(A):
        for scale in (1.0, 1e3, 1e5, 1e6):          # a count, thousands, lakh or millions, as the sheet keeps it
            if put(T, f"={cell(G)}*10^7/{cell(A)}" + (f"/{scale:.0f}" if scale != 1 else ""),
                   val(G) * 1e7 / val(A) / scale):
                break
    if have(M, MAU, MAC):                            # transacting users = active users x the MTU/MAU ratio
        put(M, f"={cell(MAU)}*{cell(MAC)}", val(MAU) * val(MAC))
    if have(F, T, M) and val(M):
        put(F, f"={cell(T)}/{cell(M)}", val(T) / val(M))
    elif have(M, T, F) and val(F):
        put(M, f"={cell(T)}/{cell(F)}", val(T) / val(F))
    if have(M, T):
        for scale in (1e6, 1e3, 1.0):
            if put(M, f"={cell(T)}/{scale:.0f}", val(T) / scale):
                break
    opd = fc.get("orders_per_day")
    if opd and month and have(opd, S, A) and val(A):
        days = calendar.monthrange(int(month[:4]), int(month[5:7]))[1]
        put(opd, f"={cell(S)}*10^7/{cell(A)}/{days}", val(S) * 1e7 / val(A) / days)

    # ---- categories
    tree = cfg.get("fashion_tree") or cfg.get("category_tree") or {}
    leaves = [lf for lf in tree.get("leaves", []) if lf.get("gmv")]
    pairs = {lf["gmv"]: lf["share"] for lf in leaves if lf.get("share")}
    for c in ((cfg.get("mis_generic") or {}).get("categories") or {}).values():
        if isinstance(c, dict) and c.get("gmv") and c.get("share"):
            pairs.setdefault(c["gmv"], c["share"])
    shares = [lf["share"] for lf in leaves if lf.get("share")]
    resid = next((lf["share"] for lf in leaves if "other" in str(lf.get("label", "")).lower()
                  and lf.get("share") in arow), None)
    if resid is not None:                            # the master closes the mix: Others = 1 - SUM(the rest)
        rest = [p for p in shares if p != resid and p in arow and val(p) is not None]
        if rest and have(resid):
            put(resid, "=1-(" + "+".join(cell(p) for p in rest) + ")", 1 - sum(val(p) for p in rest))
    if have(S) and val(S):
        for gpar, spar in pairs.items():
            if gpar not in arow or val(gpar) is None:
                continue
            if have(spar) and put(gpar, f"={cell(spar)}*{cell(S)}", val(spar) * val(S)):
                continue
            put(gpar, f"={cell(S)}*{val(gpar) / val(S):.12g}", val(gpar))      # keep this month's mix
    rolled = set()
    for par, parts in (tree.get("rollups") or {}).items():
        par = int(par)
        rolled.update(int(p) for p in parts)
        mine = [p for p in parts if p in arow and val(p) is not None]
        if par in arow and val(par) is not None and mine:
            put(par, "=" + "+".join(cell(p) for p in mine), sum(val(p) for p in mine))

    # ---- units, their shares and the platform ASP
    it_scale = fc.get("items_scale", 1.0)
    items = []
    for lf in leaves:
        it, g, a_ = lf.get("items"), lf.get("gmv"), lf.get("asp")
        if it and have(it, g, a_) and val(a_):
            put(it, f"={cell(g)}*10^7/{cell(a_)}" + (f"/{it_scale:.0f}" if it_scale != 1 else ""),
                val(g) * 1e7 / val(a_) / it_scale)
            items.append(it)
    tot = fc.get("items_total")
    mine = [p for p in items if p in arow and val(p) is not None]
    if tot and have(tot) and mine:
        put(tot, "=" + "+".join(cell(p) for p in mine), sum(val(p) for p in mine))
        for lf in leaves:
            ish = lf.get("items_share")
            if ish and have(ish) and lf.get("items") in mine and val(tot):
                put(ish, f"={cell(lf['items'])}/{cell(tot)}", val(lf["items"]) / val(tot))
    if AS is not None and have(AS):
        if tot and have(tot, S) and val(tot):                     # shipped GMV over the units sold
            put(AS, f"={cell(S)}*10^7/{cell(tot)}" + (f"/{it_scale:.0f}" if it_scale != 1 else ""),
                val(S) * 1e7 / val(tot) / it_scale)
        terms = [(lf["gmv"], lf["asp"]) for lf in leaves
                 if lf.get("asp") in arow and have(lf["gmv"], lf["asp"]) and val(lf["asp"])]
        if terms and AS not in linked:
            roots = [int(k) for k in (tree.get("rollups") or {}) if int(k) not in rolled and int(k) in arow]
            den = "+".join(f"{cell(g)}/{cell(a_)}" for g, a_ in terms)
            dv = sum(val(g) / val(a_) for g, a_ in terms)
            for num in roots + ([S] if S is not None else []):
                if have(num) and put(AS, f"={cell(num)}/({den})", val(num) / dv):
                    break

    # ---- the sheet's category units block: leaf = share x shipped x 10 / ASP, groups and the total are sums
    lay = unit_layout(ref) if ref is not None else None
    if lay and S is not None and have(S):
        vals = layout_values(lay, val, val(S))
        lc = lambda p: p is not None and p in arow and p in vals
        for e in lay["entries"]:
            if e["leaf"] and lc(e["par"]) and have(e["share"], e["asp"]):
                put(e["par"], f"={cell(e['share'])}*{cell(S)}*10/{cell(e['asp'])}", vals[e["par"]])
        for e in lay["entries"]:
            mine = [p for p in e["kids"] if lc(p)]
            if not e["leaf"] and lc(e["par"]) and mine:
                put(e["par"], "=" + "+".join(cell(p) for p in mine), sum(vals[p] for p in mine))
        top = [e["par"] for e in lay["entries"] if e["top"] and lc(e["par"])]
        tp = lay["total"]["par"]
        if top and lc(tp):
            put(tp, "=" + "+".join(cell(p) for p in top), sum(vals[p] for p in top))
            for e in lay["entries"]:
                if lc(e["par"]) and lc(e["ipar"]):
                    put(e["ipar"], f"={cell(e['par'])}/{cell(tp)}", vals[e["ipar"]])

    # ---- units per category (Mn) = GMV x 10 / ASP; a segment's total is the sum of its categories
    if ref is not None:
        by_seg: dict[Any, list[int]] = {}
        for lf in (cfg.get("l2_categories") or {}).values():
            g_, a_, u_ = (ref.row_to_par.get(lf.get(k)) for k in ("gmv", "asp", "units"))
            if have(g_, a_, u_) and val(a_) and put(u_, f"={cell(g_)}*10/{cell(a_)}", val(g_) * 10 / val(a_)):
                by_seg.setdefault(lf.get("segment"), []).append(u_)
        for sk, sv in (cfg.get("segments") or {}).items():
            tp, mine = ref.row_to_par.get(sv.get("items")), by_seg.get(sk) or []
            if mine and have(tp):
                put(tp, "=" + "+".join(cell(p) for p in mine), sum(val(p) for p in mine))

    # ---- ASP and basket from GMV and units: segment ASP = segment GMV x 10 / its units; platform ASP = shipped
    #      GMV x 10 / all units; basket = units per transaction. Each only where it reproduces the figure.
    if ref is not None:
        seg_units = []
        for sv in (cfg.get("segments") or {}).values():
            g_, a_, u_ = (ref.row_to_par.get(sv.get(k)) for k in ("shipped_gmv", "asp", "items"))
            if have(g_, u_) and val(u_):
                if a_ is not None and have(a_):
                    put(a_, f"={cell(g_)}*10/{cell(u_)}", val(g_) * 10 / val(u_))
                seg_units.append(u_)
        lay_ = unit_layout(ref)
        tot_u = lay_["total"]["par"] if lay_ and lay_["total"]["par"] in arow else None
        cands = []
        if seg_units and all(have(u) for u in seg_units):
            cands.append(("+".join(cell(u) for u in seg_units), sum(val(u) for u in seg_units)))
        if tot_u is not None and have(tot_u):
            cands.append((cell(tot_u), val(tot_u)))
        for den, dv in cands:
            if AS is not None and have(AS, S) and dv and put(AS, f"={cell(S)}*10/({den})", val(S) * 10 / dv, 5e-3):
                break

    # ---- basket size = AOV / ASP (items per order); the App and Web MAU = MAU x their share
    bk = P("basket") if P("basket") is not None else 519
    if A is not None and AS is not None and have(bk, A, AS) and val(AS):
        put(bk, f"={cell(A)}/{cell(AS)}", val(A) / val(AS), 2e-2)
    if have(52):
        for par_, share_ in ((2588, 163), (2590, 162)):
            if have(par_, share_):
                put(par_, f"={cell(52)}*{cell(share_)}", val(52) * val(share_))

    # ---- price buckets: a bucket is its share x shipped GMV, so together they equal shipped GMV
    mg = cfg.get("mis_generic") or {}
    bucket_pars = {int(p) for p in (mg.get("price_buckets") or {}).values()}
    for v_ in (mg.get("unsplit_buckets") or {}).values():
        bucket_pars.update(int(p) for p in v_)
    live_b = [p for p in sorted(bucket_pars) if p in arow and val(p) is not None]
    if len(live_b) >= 2 and have(S) and val(S) and abs(sum(val(p) for p in live_b) - val(S)) <= 1e-6 * max(1.0, val(S)):
        for p in live_b:
            put(p, f"={cell(S)}*{val(p) / val(S):.12g}", val(p))

    # ---- splits: the last member present is 1 - the others
    groups = [list(g) for g in (cfg.get("split_groups") or {}).get("groups", [])] + [list(g) for g in KNOWN_SPLITS]
    for g in groups:
        present = [p for p in g if p in arow and val(p) is not None]
        if len(present) >= 2:
            tgt, others = present[-1], present[:-1]
            put(tgt, "=1-(" + "+".join(cell(p) for p in others) + ")", 1 - sum(val(p) for p in others))
    for tgt, src in (cfg.get("mirrors") or {}).items():          # the BPC rows repeat the platform rows
        if str(tgt).isdigit() and have(int(src)):
            put(int(tgt), f"={cell(int(src))}", val(int(src)))
    return len(linked)


def _analysis_row(ws, r, sno, t, hist, yhist, cur, v, lay, rel_rngs, pp_rngs) -> None:
    """One metric's row on the Analysis sheet: identity, the past months, this month, growth and averages.

    `lay` is the column layout: c_yag, c_rec, c_mom, c_yoy, c_avg (first columns of each block), n, width."""
    L = get_column_letter
    c_yag, c_rec, c_mom, c_yoy, c_avg, n = (lay[k] for k in ("c_yag", "c_rec", "c_mom", "c_yoy", "c_avg", "n"))
    cur_col = L(c_rec + n - 1)
    nhead = lay["width"]
    pct = (t.unit == "%")
    ws.cell(r, 1, t.par_id).font = TD_MUTED
    ws.cell(r, 2, sno).font = TD_MUTED
    ws.cell(r, 3, t.parameter_name.strip()).font = TD
    ws.cell(r, 4, t.unit or "").font = TD
    vfmt = "0.00%" if pct else ("#,##0.00" if (t.unit or "") in ("INR Cr", "Mn", "#", "") else "#,##0")
    gfmt = '0.0" pp"' if pct else "0.0%"
    for j, x in enumerate(hist + [cur]):
        c = ws.cell(r, c_rec + j, x)
        c.number_format, c.font = vfmt, (TD_B if j == n - 1 else TD)
        if j == n - 1:
            c.fill = PatternFill("solid", fgColor="FFF6F7")
    for j, x in enumerate(yhist):
        c = ws.cell(r, c_yag + j, x)
        c.number_format, c.font = vfmt, TD
    grow = (lambda a, b: f'=IF(AND(ISNUMBER({a}),ISNUMBER({b})),({a}-{b})*100,"")') if pct else \
           (lambda a, b: f'=IF(AND(ISNUMBER({a}),ISNUMBER({b}),{b}<>0),{a}/{b}-1,"")')
    for j in range(1, n):
        a, b = f"{L(c_rec + j)}{r}", f"{L(c_rec + j - 1)}{r}"
        c = ws.cell(r, c_mom + j - 1, grow(a, b))
        c.number_format, c.font = gfmt, TD
    for j in range(n):
        a, b = f"{L(c_rec + j)}{r}", f"{L(c_yag + j)}{r}"
        c = ws.cell(r, c_yoy + j, grow(a, b))
        c.number_format, c.font = gfmt, TD
    last3 = f"{L(c_rec + n - 4)}{r}:{L(c_rec + n - 2)}{r}"
    last5 = f"{L(c_rec)}{r}:{L(c_rec + n - 2)}{r}"
    cur_c = f"{cur_col}{r}"
    for k, (rng, kind) in enumerate(((last3, "avg"), (last5, "avg"), (last3, "vs"), (last5, "vs"))):
        if kind == "avg":
            f_ = f'=IFERROR(AVERAGE({rng}),"")'
            fm = vfmt
        else:
            a = f"AVERAGE({rng})"
            f_ = (f'=IFERROR(IF(ISNUMBER({cur_c}),({cur_c}-{a})*100,""),"")' if pct else
                  f'=IFERROR(IF(ISNUMBER({cur_c}),{cur_c}/{a}-1,""),"")')
            fm = gfmt
        c = ws.cell(r, c_avg + k, f_)
        c.number_format, c.font = fm, TD
    if v is not None and not v.is_null:
        kind, color = DOT[v.provenance]
        ws.cell(r, c_avg + 4, kind).font = Font(name="Calibri", size=10, color=color)
    for i in range(1, nhead + 1):
        ws.cell(r, i).border = BOX
    (pp_rngs if pct else rel_rngs).extend([
        f"{L(c_mom)}{r}:{L(c_mom + n - 2)}{r}", f"{L(c_yoy)}{r}:{L(c_yoy + n - 1)}{r}",
        f"{L(c_avg + 2)}{r}:{L(c_avg + 3)}{r}"])


def write_analysis_workbook(path: str | Path, ctx: dict[str, Any], n_months: int = 5) -> Path:
    """The month beside its recent past and the same months a year earlier, with MoM and YoY.

    Rows follow the output sheet (par_id, S.No, Parameter, Units), then month columns as in the
    reference sheets. Growth and averages are Excel formulas, so editing any figure here
    updates them. A percentage metric's growth is the change in percentage points.
    """
    ref, est, month, cfg = ctx["reference"], ctx["estimate"], ctx["month"], ctx["config"]
    rec = [shift_months(month, -i) for i in range(n_months, -1, -1)]        # M-5 .. M
    yag = [shift_months(m, -12) for m in rec]                               # the same months, a year earlier
    n = len(rec)

    wb = Workbook()
    ws = wb.active
    ws.title = "Analysis"
    ws.sheet_view.showGridLines = False
    ws.cell(1, 1, f"{cfg['display_name']}: {month_label(month)} against the last {n_months} months "
                  "and the same months last year").font = H1
    ws.cell(2, 1, "Growth, averages and gaps are formulas: change a figure and they update. "
                  "So are shipped, net and category GMV (from Gross GMV, cancellation, returns and the "
                  "category shares): edit Gross GMV and they follow. "
                  "Percentage metrics show growth in percentage points (pp).").font = H2

    # column map: 1-4 identity, then blocks
    c_yag = 5                                   # last year comes first, as the reference lays years out
    c_rec = c_yag + n + 1
    c_mom = c_rec + n + 1
    c_yoy = c_mom + (n - 1) + 1
    c_avg = c_yoy + n + 1
    L = get_column_letter

    blocks = [(c_yag, n, "Same months last year"),
              (c_rec, n, f"Last {n_months} months and {month_label(month)}"),
              (c_mom, n - 1, "MoM growth"),
              (c_yoy, n, "YoY growth"),
              (c_avg, 5, f"{month_label(month)} against the earlier months")]
    for c0, w, label in blocks:
        cell = ws.cell(4, c0, label)
        cell.font, cell.fill = TH, PatternFill("solid", fgColor="2F5597")
        cell.alignment = Alignment(horizontal="center")
        ws.merge_cells(start_row=4, start_column=c0, end_row=4, end_column=c0 + w - 1)

    heads = (["par_id", "S.No", "Parameter", "Units"] + [month_label(m) for m in yag] + [""]
             + [month_label(m) for m in rec] + [""]
             + [f"{month_label(m)} vs {month_label(shift_months(m, -1))}" for m in rec[1:]] + [""]
             + [f"{month_label(m)} vs {month_label(y)}" for m, y in zip(rec, yag)] + [""]
             + [f"Avg of last 3", f"Avg of last 5", "vs avg of last 3", "vs avg of last 5", "This month's source"])
    for i, h in enumerate(heads, 1):
        c = ws.cell(5, i, h)
        c.font, c.fill = TH, FILL_HEAD
        c.alignment = Alignment(horizontal="center" if i > 4 else "left", vertical="center", wrap_text=True)
    ws.row_dimensions[5].height = 32
    widths = {1: 8, 2: 6, 3: 44, 4: 9}
    for i in range(1, len(heads) + 1):
        ws.column_dimensions[L(i)].width = widths.get(i, 11 if heads[i - 1] else 2)
    ws.column_dimensions[L(len(heads))].width = 16
    ws.freeze_panes = ws.cell(6, 5)

    lay = {"c_yag": c_yag, "c_rec": c_rec, "c_mom": c_mom, "c_yoy": c_yoy, "c_avg": c_avg, "n": n,
           "width": len(heads)}
    r, sno = 6, 0
    rel_rngs: list[str] = []                     # growth ranges on value metrics, for colouring
    pp_rngs: list[str] = []                      # and on percentage metrics (points)
    last_sec = last_q = None
    arow: dict[int, int] = {}                    # par_id -> its row here
    curv: dict[int, Any] = {}                    # par_id -> this month's figure, for the formulas
    grp_rows: list[int] = []                     # section banner rows, shaded across the period blocks too
    cur_col = L(c_rec + n - 1)
    for t, sec in analysis_template(ref, est, cfg):
        if sec == "Not available this month":
            continue
        row_ref = ref.par_to_row.get(t.par_id)
        dh = derived_history(ref, cfg).get(t.par_id, {})
        hist = [_hist_val(ref, row_ref, dh, m) for m in rec[:-1]]
        yhist = [_hist_val(ref, row_ref, dh, m) for m in yag]
        v = est.value_for_par(ref, t.par_id)
        cur = v.value if v is not None and not v.is_null else None
        if cur is None and not any(x is not None for x in hist + yhist):
            continue
        if sec != last_sec:
            last_sec, last_q = sec, None
            for i in range(1, len(heads) + 1):
                ws.cell(r, i).fill = FILL_GROUP
            ws.cell(r, 3, sec).font = GROUP
            grp_rows.append(r)
            r += 1
        q = (t.parent_question or "").strip()
        if q and q != last_q:
            last_q = q
            ws.cell(r, 3, q).font = TD_B
            r += 1
        sno += 1
        arow[t.par_id] = r
        curv[t.par_id] = cur
        _analysis_row(ws, r, sno, t, hist, yhist, cur, v, lay, rel_rngs, pp_rngs)
        r += 1

    add_growth_colours(ws, rel_rngs, pp_rngs)
    _link_formulas(ws, arow, cur_col, curv, cfg, month, ref)
    add_deviation_sheets(wb, ctx)
    months_, col_of, layout = _history_sheets(wb, ref, est, cfg, month)
    _period_sheets(wb, cfg, month, months_, col_of, layout)
    _trend_block(ws, arow, grp_rows, c_mom, c_yoy, n, c_avg + 6, r - 1)
    _period_blocks(ws, cfg, month, months_, col_of, layout, arow, grp_rows, rec, c_rec,
                   c_start=c_avg + 9, last_row=r - 1)
    _run_data_sheet(wb, ctx)
    wb.save(path)
    return Path(path)


# ================================================================== deviation

def _flag_colours(ws: Worksheet, rng: str) -> None:
    """Review in red, OK in green."""
    from openpyxl.formatting.rule import CellIsRule
    ws.conditional_formatting.add(rng, CellIsRule(
        operator="equal", formula=['"Review"'], fill=PatternFill("solid", bgColor=RYG_RED),
        font=Font(bold=True, color="FFFFFF")))
    ws.conditional_formatting.add(rng, CellIsRule(
        operator="equal", formula=['"OK"'], fill=PatternFill("solid", bgColor=RYG_GREEN),
        font=Font(color="1F4D2E")))


def _mis_figure(ref, mis_only, t, v) -> float | None:
    """What the MIS reports (or implies) for a metric, or None when it gives nothing for it."""
    mo = mis_only.value_for_par(ref, t.par_id) if mis_only is not None else None
    if v.uncalibrated is not None:
        return v.uncalibrated
    if mo is not None and not mo.is_null and mo.value is not None and \
            mo.provenance in (Provenance.REPORTED, Provenance.DERIVED):
        return mo.value
    if v.provenance in (Provenance.REPORTED, Provenance.DERIVED):
        return v.value
    return None


def _deviation_detail(ws: Worksheet, r: int, ctx: dict[str, Any], limit: float) -> int:
    """Every metric, grouped by section and category: absolutes, MoM and YoY, our output against the MIS.

    Absolutes: last month and the same month last year (as stored), the MIS figure, our output,
    the gap and the deviation. MoM and YoY are shown for the MIS figure and for ours, and the
    difference between them in points, so a category can be judged on level and on direction.
    """
    ref, est, month, cfg = ctx["reference"], ctx["estimate"], ctx["month"], ctx["config"]
    mis_only, has_mis = ctx.get("mis_only"), ctx.get("has_mis", False)
    prev_m, yago_m = shift_months(month, -1), shift_months(month, -12)
    L = get_column_letter
    heads = ["Parameter", "par_id", "Unit",
             month_label(prev_m), month_label(yago_m), "MIS figure", "Our output", "Gap", "Deviation",
             "MIS", "Ours", "Difference", "MIS", "Ours", "Difference", "Check"]
    n = len(heads)
    blocks = [(4, 2, "Stored"), (6, 4, f"{month_label(month)} absolutes"),
              (10, 3, f"MoM vs {month_label(prev_m)}"), (13, 3, f"YoY vs {month_label(yago_m)}")]
    for c0, w, text in blocks:
        c = ws.cell(r, c0, text)
        c.font, c.fill = TH, PatternFill("solid", fgColor="2F5597")
        c.alignment = Alignment(horizontal="center")
        ws.merge_cells(start_row=r, start_column=c0, end_row=r, end_column=c0 + w - 1)
    r += 1
    for i, h in enumerate(heads, 1):
        c = ws.cell(r, i, h)
        c.font, c.fill = TH, FILL_HEAD
        c.alignment = Alignment(horizontal="left" if i == 1 else "center", vertical="center", wrap_text=True)
    ws.row_dimensions[r].height = 30
    head_row = r
    for i, w in enumerate([46, 10, 10, 11, 11, 13, 13, 12, 11, 10, 10, 11, 10, 10, 11, 9, 70], 1):
        ws.column_dimensions[L(i)].width = w
    ws.cell(head_row, n + 1, "How our figure was set").font = TH
    ws.cell(head_row, n + 1).fill = FILL_HEAD
    r += 1

    dev_rel, dev_pp, diff_rngs, first = [], [], [], r
    last_sec = last_q = None
    for t, sec in analysis_template(ref, est, cfg):
        if sec == "Not available this month":
            continue
        v = est.value_for_par(ref, t.par_id)
        if v is None or v.is_null or v.value is None:
            continue
        if sec != last_sec:
            last_sec, last_q = sec, None
            for i in range(1, n + 2):
                ws.cell(r, i).fill = FILL_GROUP
            ws.cell(r, 1, sec).font = GROUP
            r += 1
        q = (t.parent_question or "").strip()
        if q and q != last_q:
            last_q = q
            ws.cell(r, 1, q).font = TD_B
            r += 1
        pct = t.unit == "%"
        row = ref.par_to_row.get(t.par_id)
        prev = ref.value(row, prev_m) if row else None
        yago = ref.value(row, yago_m) if row else None
        mis_v = _mis_figure(ref, mis_only, t, v) if has_mis else None
        vf = "0.00%" if pct else "#,##0.00"
        gf = '0.0" pp"' if pct else "0.0%"
        # growth: a fraction for a value metric, points for a percentage one
        g = (lambda a, b: f'=IF(AND(ISNUMBER({a}),ISNUMBER({b})),({a}-{b})*100,"")') if pct else \
            (lambda a, b: f'=IF(AND(ISNUMBER({a}),ISNUMBER({b}),{b}<>0),{a}/{b}-1,"")')
        diff = (lambda a, b: f'=IF(AND(ISNUMBER({a}),ISNUMBER({b})),{a}-{b},"")') if pct else \
               (lambda a, b: f'=IF(AND(ISNUMBER({a}),ISNUMBER({b})),({a}-{b})*100,"")')
        gap = f'=IF(ISNUMBER(F{r}),(G{r}-F{r})*100,"")' if pct else f'=IF(ISNUMBER(F{r}),G{r}-F{r},"")'
        dev = f'=IF(ISNUMBER(H{r}),H{r},"")' if pct else f'=IFERROR(G{r}/F{r}-1,"")'
        chk = f'=IF(ISNUMBER(I{r}),IF(ABS(I{r})>{2 if pct else limit},"Review","OK"),"")'
        vals = [t.parameter_name.strip(), t.par_id, t.unit, prev, yago, mis_v, v.value, gap, dev,
                g(f"F{r}", f"D{r}"), g(f"G{r}", f"D{r}"), diff(f"K{r}", f"J{r}"),
                g(f"F{r}", f"E{r}"), g(f"G{r}", f"E{r}"), diff(f"N{r}", f"M{r}"), chk, v.method]
        fmts = {4: vf, 5: vf, 6: vf, 7: vf, 8: '0.00" pp"' if pct else "#,##0.00", 9: '0.0" pp"' if pct else "0.0%",
                10: gf, 11: gf, 12: '0.0" pp"', 13: gf, 14: gf, 15: '0.0" pp"'}
        _row(ws, r, vals, aligns={2: "center", 3: "center", 16: "center"}, formats=fmts,
             fonts={7: TD_B, 1: TD})
        ws.cell(r, 1).alignment = Alignment(indent=1)
        (dev_pp if pct else dev_rel).append(f"I{r}")
        diff_rngs += [f"L{r}", f"O{r}"]
        r += 1
    if r > first:
        _flag_colours(ws, f"P{first}:P{r}")
        add_growth_colours(ws, dev_rel, dev_pp, rel_span=0.10, pp_span=2.0)
        add_growth_colours(ws, [], diff_rngs, pp_span=5.0)
        ws.auto_filter.ref = f"A{head_row}:{L(n + 1)}{r - 1}"
        ws.freeze_panes = ws.cell(head_row + 1, 2)
    return r


def add_deviation_sheets(wb: Workbook, ctx: dict[str, Any], limit: float = 0.05) -> None:
    """Add the deviation sheets (Deviation, Vs MIS, Vs expert call) to a workbook.

    How far the output sits from the month's MIS and from the expert call. Gaps are formulas.
    Anything beyond `limit` (5%, or 2 points for a percentage metric) is marked for review.
    """
    ref, est, month, cfg = ctx["reference"], ctx["estimate"], ctx["month"], ctx["config"]
    mis_only, tr, cal = ctx.get("mis_only"), ctx.get("transcript"), ctx.get("calibration")
    has_mis, label = ctx.get("has_mis", False), month_label(month)
    ws0, r0 = _sheet(wb, "Deviation", f"{cfg['display_name']}: {label} against the MIS and the expert call")

    # ---- Vs MIS ----------------------------------------------------------------
    ws, r = _sheet(wb, "Vs MIS", "Our output against the MIS",
                   "MIS figure = what the MIS reports, or what it implies. Deviation is our output against it; "
                   "percentage metrics show the gap in points.")
    hdr = _head(ws, r, [("par_id", 8), ("Parameter", 46), ("Unit", 9), ("MIS figure", 14),
                        ("Our output", 14), ("Gap", 12), ("Deviation", 12), ("Check", 10),
                        ("How our figure was set", 70)], aligns={1: "center", 3: "center", 8: "center"})
    rows_n, off, big, rel_r, pp_r = 0, [], 0, [], []
    if not has_mis:
        ws.cell(hdr, 2, f"No MIS was supplied for {label}, so there is nothing to compare against.").font = TD_MUTED
    else:
        i = r2 = hdr
        for t, sec in analysis_template(ref, est, cfg):
            v = est.value_for_par(ref, t.par_id)
            if v is None or v.is_null or v.value is None:
                continue
            mis_v = _mis_figure(ref, mis_only, t, v)
            if mis_v is None:
                continue
            pct = t.unit == "%"
            sc = 100.0 if pct else 1.0
            k = r2
            gap = f"=IF(ISNUMBER(D{k}),(E{k}-D{k})*100,\"\")" if pct else f"=E{k}-D{k}"
            dev = f'=IFERROR(E{k}/D{k}-1,"")' if not pct else f'=IF(ISNUMBER(F{k}),F{k},"")'
            chk = (f'=IF(ISNUMBER(G{k}),IF(ABS(G{k})>{2 if pct else limit},"Review","OK"),"")')
            _row(ws, k, [t.par_id, t.parameter_name.strip(), t.unit, mis_v, v.value, gap, dev, chk,
                         v.method], band=bool(rows_n % 2),
                 aligns={1: "center", 3: "center", 8: "center"},
                 formats={4: "0.00%" if pct else "#,##0.00", 5: "0.00%" if pct else "#,##0.00",
                          6: '0.00" pp"' if pct else "#,##0.00", 7: '0.0" pp"' if pct else "0.0%"})
            (pp_r if pct else rel_r).append(f"G{k}")
            if (pct and abs(v.value - mis_v) * 100 > 2) or (not pct and mis_v and abs(v.value / mis_v - 1) > limit):
                big += 1
            rows_n += 1
            r2 += 1
        ws.auto_filter.ref = f"A{hdr - 1}:I{max(r2 - 1, hdr)}"
        _flag_colours(ws, f"H{hdr}:H{max(r2, hdr)}")
        add_growth_colours(ws, rel_r, pp_r, rel_span=0.10, pp_span=2.0)

    # ---- Vs expert call ----------------------------------------------------
    we, r = _sheet(wb, "Vs expert call", "Our output against the expert call",
                   "Category growth the expert gave, against the growth our output implies on the stored "
                   "year-ago month. Difference is in points.")
    exp_rows = ctx.get("expert_rows") or []
    gross = est.value_for_par(ref, 241)
    if tr is None:
        we.cell(r, 1, f"No expert call was supplied for {label}.").font = TD_MUTED
    else:
        r = _head(we, r, [("Category", 40), ("Call YoY", 12), ("Our YoY", 12), ("Difference", 12),
                          ("Check", 10), ("Our category is", 18)], aligns={5: "center"})
        first, rel2 = r, []
        for e in sorted(exp_rows, key=lambda x: -abs(x["gap"])):
            _row(we, r, [e["label"], e["call_yoy"], e["out_yoy"], f"=(B{r}-C{r})*100",
                         f'=IF(ABS(D{r})>10,"Review","OK")',
                         "estimated" if e["estimated"] else "reported"],
                 band=bool((r - first) % 2), aligns={5: "center"},
                 formats={2: "0.0%", 3: "0.0%", 4: '0.0" pp"'})
            rel2.append(f"D{r}")
            r += 1
        if exp_rows:
            _flag_colours(we, f"E{first}:E{r}")
            add_growth_colours(we, [], rel2, pp_span=15.0)
        else:
            we.cell(r, 1, "The call gave no category growth that matches this player's categories.").font = TD_MUTED
            r += 1
        r += 1
        we.cell(r, 1, "Figures the expert stated").font = GROUP
        r += 1
        r = _head_at(we, r, ["Stated figure", "Expert", "Our figure", "Deviation"])
        gcur = gross.value if gross is not None and not gross.is_null else None
        grow = ref.par_to_row.get(241)
        for k_, val in (tr.stated_values or {}).items():
            ours = None
            if k_ == "Platform GMV":
                ours = gcur
            elif k_.endswith(" GMV") and grow:
                for m in ref.months:
                    if month_label(m).replace("'", " 20") == k_[:-4] or _pretty(m) == k_[:-4]:
                        ours = ref.value(grow, m)
            _row(we, r, [k_, val, ours, f'=IFERROR(C{r}/B{r}-1,"")' if ours is not None else ""],
                 formats={2: "#,##0.00", 3: "#,##0.00", 4: "0.0%"})
            r += 1
        if cal is not None and cal.implied:
            r += 1
            we.cell(r, 1, "What the call implies about the stored history").font = GROUP
            r += 1
            r = _head_at(we, r, ["Category", "MIS this month", "Call YoY", "Implied year-ago",
                                 "Stored year-ago", "Stored vs implied"])
            for x in cal.implied:
                _row(we, r, [x.category, x.mis_value, x.call_yoy, x.implied_prior, x.stored_prior,
                             f"=E{r}/D{r}-1"], formats={2: "#,##0.00", 3: "0.0%", 4: "#,##0.00",
                                                       5: "#,##0.00", 6: "0.0%"})
                r += 1

    # ---- Summary -----------------------------------------------------------
    rc = est.reconciliations.get(241) if hasattr(est, "reconciliations") else None
    lines = [("Month", label), ("MIS supplied", "Yes" if has_mis else "No"),
             ("Expert call supplied", "Yes" if tr is not None else "No")]
    if rc is not None and rc.level and rc.final_level:
        lines += [("Gross GMV, MIS reports (INR Cr)", round(rc.level, 2)),
                  ("Gross GMV, our output (INR Cr)", round(rc.final_level, 2)),
                  ("Output against the MIS", f"{rc.final_level / rc.level - 1:+.1%}"),
                  ("History weight used", f"{rc.blend_weight:.0%}" if rc.blend_weight is not None else "")]
    elif gross is not None and not gross.is_null:
        lines.append(("Gross GMV, our output (INR Cr)", round(gross.value, 2)))
    lines += [("Metrics compared with the MIS", rows_n), ("Of which beyond the limit", big),
              ("Categories compared with the call", len(exp_rows))]
    r = 3
    for k_, v_ in lines:
        ws0.cell(r, 1, k_).font = TD_B
        ws0.cell(r, 2, v_).font = TD
        r += 1
    r += 1
    ws0.cell(r, 1, "Detail by category and sub-category").font = GROUP
    ws0.cell(r + 1, 1, "Levels, MoM and YoY for every metric. The MIS columns are blank where the MIS gives "
                       "no figure; growth is in points for a percentage metric.").font = H2
    _deviation_detail(ws0, r + 3, ctx, limit)


def _pretty(m: str) -> str:
    y, mm = m.split("-")
    return f"{_dt.date(int(y), int(mm), 1):%b} {y}"


def _head_at(ws: Worksheet, row: int, labels: list[str]) -> int:
    for i, h in enumerate(labels, 1):
        c = ws.cell(row, i, h)
        c.font, c.fill = TH, FILL_HEAD
    return row + 1


def _history_sheets(wb: Workbook, ref, est, cfg: dict, month: str) -> None:
    """The whole stored series for every metric, with MoM and YoY across it.

    This is the reference workbook's history (your upload, or the built-in one, plus any saved
    months), with this month's figure in the last column, so anything can be looked back on.
    """
    months = sorted(set(ref.months) | {month})
    months = [m for m in months if m <= month]
    col_of = {m: 5 + i for i, m in enumerate(months)}
    L = get_column_letter
    rows = []
    for t, sec in analysis_template(ref, est, cfg):
        if sec == "Not available this month":
            continue
        row = ref.par_to_row.get(t.par_id)
        v = est.value_for_par(ref, t.par_id)
        cur = v.value if v is not None and not v.is_null else None
        dh = derived_history(ref, cfg).get(t.par_id, {})
        series = {m: _hist_val(ref, row, dh, m) for m in months[:-1]}
        series = {m: x for m, x in series.items() if x is not None}
        if cur is None and not series:
            continue
        rows.append((t, sec, series, cur))

    def start(title: str, note: str):
        ws = wb.create_sheet(title)
        ws.sheet_view.showGridLines = False
        ws.cell(1, 1, f"{cfg['display_name']}: {title.lower()}, "
                      f"{month_label(months[0])} to {month_label(month)}").font = H1
        ws.cell(2, 1, note).font = H2
        for i, h in enumerate(["par_id", "S.No", "Parameter", "Units"] + [month_label(m) for m in months], 1):
            c = ws.cell(4, i, h)
            c.font, c.fill = TH, FILL_HEAD
            c.alignment = Alignment(horizontal="center" if i > 4 else "left")
        for i, w in {1: 8, 2: 6, 3: 56, 4: 9}.items():
            ws.column_dimensions[L(i)].width = w
        for m in months:
            ws.column_dimensions[L(col_of[m])].width = 10
        ws.freeze_panes = "E5"
        return ws

    hv = start("History", "Every stored month for each metric, as the reference holds it. "
                          f"{month_label(month)} is this run's figure."
                          + (" Category shipped GMV for earlier months is category share x shipped GMV "
                             "(this player's sheet stores shares)." if derived_history(ref, cfg) else ""))
    hm = start("History MoM", "Each month against the month before. Percentage metrics: change in points.")
    hy = start("History YoY", "Each month against the same month a year earlier. "
                              "Percentage metrics: change in points.")
    rel_m, pp_m, rel_y, pp_y = [], [], [], []
    layout = []                                  # where each metric sits on the History sheet
    r, sno, last_sec = 5, 0, None
    for t, sec, series, cur in rows:
        if sec != last_sec:
            last_sec = sec
            for ws_ in (hv, hm, hy):
                for i in range(1, 5 + len(months)):
                    ws_.cell(r, i).fill = FILL_GROUP
                ws_.cell(r, 3, sec).font = GROUP
            r += 1
        sno += 1
        layout.append({"par": t.par_id, "name": t.parameter_name.strip(), "unit": t.unit or "",
                       "sec": sec, "row": r})
        pct = t.unit == "%"
        vfmt = "0.00%" if pct else ("#,##0.00" if (t.unit or "") in ("INR Cr", "Mn", "#", "") else "#,##0")
        gfmt = '0.0" pp"' if pct else "0.0%"
        ctx_label = t.parameter_name.strip()
        if sec == "Key metrics" and (t.parent_question or "").strip():
            ctx_label += f" | {t.parent_question.strip()}"          # what the figure belongs to
        for ws_ in (hv, hm, hy):
            for i, x in enumerate([t.par_id, sno, ctx_label, t.unit or ""], 1):
                c = ws_.cell(r, i, x)
                c.font = TD_MUTED if i <= 2 else TD
        for m in months:
            x = cur if m == month else series.get(m)
            c = hv.cell(r, col_of[m], x)
            c.number_format, c.font = vfmt, (TD_B if m == month else TD)
            for ws_, back in ((hm, -1), (hy, -12)):
                pm = shift_months(m, back)
                if pm not in col_of:
                    continue
                a, b = f"History!{L(col_of[m])}{r}", f"History!{L(col_of[pm])}{r}"
                f_ = (f'=IF(AND(ISNUMBER({a}),ISNUMBER({b})),({a}-{b})*100,"")' if pct else
                      f'=IF(AND(ISNUMBER({a}),ISNUMBER({b}),{b}<>0),{a}/{b}-1,"")')
                c = ws_.cell(r, col_of[m], f_)
                c.number_format, c.font = gfmt, TD
        span = f"{L(5)}{r}:{L(4 + len(months))}{r}"
        (pp_m if pct else rel_m).append(span)
        (pp_y if pct else rel_y).append(span)
        for ws_ in (hv, hm, hy):
            for i in range(1, 5 + len(months)):
                ws_.cell(r, i).border = BOX
        r += 1
    add_growth_colours(hm, rel_m, pp_m)
    add_growth_colours(hy, rel_y, pp_y)
    return months, col_of, layout


# ================================================================== quarterly and half-yearly

def _fy_key(m: str, half: bool) -> tuple[int, int]:
    """(fiscal year, quarter or half) for a month: the fiscal year runs April to March."""
    y, mo = int(m[:4]), int(m[5:7])
    fy = y + 1 if mo >= 4 else y
    q = ((mo - 4) % 12) // 3 + 1
    return (fy, (1 if q <= 2 else 2) if half else q)


def _fy_label(key: tuple[int, int], half: bool) -> str:
    return f"{'H' if half else 'Q'}{key[1]} FY{key[0] % 100:02d}"


def _agg_of(name: str, unit: str) -> str:
    """Money and counts of orders add up over a period; rates and levels are averaged."""
    low = name.lower()
    if unit == "INR Cr":
        return "SUM"
    if unit == "Mn" and any(k in low for k in ("transaction", "order", "unit", "shipment")):
        return "SUM"
    return "AVERAGE"


def _period_sheets(wb: Workbook, cfg: dict, month: str, months: list[str],
                   col_of: dict[str, int], layout: list[dict]) -> None:
    """Quarterly and half-yearly views, built by formula from the History sheet."""
    L = get_column_letter
    for half, title, keep, lag, chg in ((False, "Quarterly", 12, 4, "QoQ"), (True, "Half-yearly", 6, 2, "HoH")):
        span = 6 if half else 3
        cur_key = _fy_key(month, half)
        all_keys = sorted({_fy_key(m, half) for m in months})
        full = {k: [m for m in months if _fy_key(m, half) == k] for k in all_keys}
        # a period needs all its months, except the one still running; history may start mid-period
        keys = [k for k in all_keys if len(full[k]) == span or k == cur_key][-keep:]
        by_key = {k: full[k] for k in keys}
        partial = len(by_key[cur_key]) < span if cur_key in by_key else False

        ws = wb.create_sheet(title)
        ws.sheet_view.showGridLines = False
        ws.cell(1, 1, f"{cfg['display_name']}: {title.lower()} view, "
                      f"{_fy_label(keys[0], half)} to {_fy_label(keys[-1], half)}").font = H1
        ws.cell(2, 1, "Fiscal year, April to March. GMV and order counts are summed over the period; "
                      "rates and levels are averaged. A period with a month missing is left blank"
                      + (f"; {_fy_label(cur_key, half)} is to {month_label(month)} so far and its growth "
                         "is not shown for summed metrics." if partial else ".")).font = H2

        c_val = 6
        c_chg = c_val + len(keys) + 1
        c_yoy = c_chg + (len(keys) - 1) + 1
        blocks = [(c_val, len(keys), f"{title} values"), (c_chg, len(keys) - 1, f"{chg} growth"),
                  (c_yoy, len(keys) - lag, "YoY growth")]
        for c0, w, label in blocks:
            if w <= 0:
                continue
            cell = ws.cell(4, c0, label)
            cell.font, cell.fill = TH, PatternFill("solid", fgColor="2F5597")
            cell.alignment = Alignment(horizontal="center")
            if w > 1:
                ws.merge_cells(start_row=4, start_column=c0, end_row=4, end_column=c0 + w - 1)
        heads = (["par_id", "S.No", "Parameter", "Units", "Basis"]
                 + [_fy_label(k, half) + (" (to date)" if (k == cur_key and partial) else "") for k in keys] + [""]
                 + [f"{_fy_label(keys[i], half)} vs {_fy_label(keys[i - 1], half)}" for i in range(1, len(keys))] + [""]
                 + [f"{_fy_label(keys[i], half)} vs {_fy_label(keys[i - lag], half)}" for i in range(lag, len(keys))])
        for i, h in enumerate(heads, 1):
            c = ws.cell(5, i, h)
            c.font, c.fill = TH, FILL_HEAD
            c.alignment = Alignment(horizontal="center" if i > 4 else "left", vertical="center", wrap_text=True)
        ws.row_dimensions[5].height = 32
        for i, w in {1: 8, 2: 6, 3: 44, 4: 9, 5: 9}.items():
            ws.column_dimensions[L(i)].width = w
        for i in range(6, len(heads) + 1):
            ws.column_dimensions[L(i)].width = 13 if heads[i - 1] else 2
        ws.freeze_panes = "F6"

        rel_r: list[str] = []
        pp_r: list[str] = []
        r, sno, last_sec = 6, 0, None
        for item in layout:
            if item["sec"] != last_sec:
                last_sec = item["sec"]
                for i in range(1, len(heads) + 1):
                    ws.cell(r, i).fill = FILL_GROUP
                ws.cell(r, 3, item["sec"]).font = GROUP
                r += 1
            sno += 1
            pct = item["unit"] == "%"
            agg = _agg_of(item["name"], item["unit"])
            vfmt = "0.00%" if pct else ("#,##0.00" if item["unit"] in ("INR Cr", "Mn", "#", "") else "#,##0")
            gfmt = '0.0" pp"' if pct else "0.0%"
            for i, x in enumerate([item["par"], sno, item["name"], item["unit"],
                                   "Sum" if agg == "SUM" else "Avg"], 1):
                c = ws.cell(r, i, x)
                c.font = TD_MUTED if i <= 2 else TD
            for j, k in enumerate(keys):
                refs = ",".join(f"History!{L(col_of[m])}{item['row']}" for m in by_key[k])
                c = ws.cell(r, c_val + j,
                            f'=IF(COUNT({refs})<{len(by_key[k])},"",{agg}({refs}))')
                c.number_format, c.font = vfmt, (TD_B if k == cur_key else TD)
            grow = (lambda a, b: f'=IF(AND(ISNUMBER({a}),ISNUMBER({b})),({a}-{b})*100,"")') if pct else \
                   (lambda a, b: f'=IF(AND(ISNUMBER({a}),ISNUMBER({b}),{b}<>0),{a}/{b}-1,"")')
            for i in range(1, len(keys)):
                if partial and agg == "SUM" and keys[i] == cur_key:
                    continue
                c = ws.cell(r, c_chg + i - 1, grow(f"{L(c_val + i)}{r}", f"{L(c_val + i - 1)}{r}"))
                c.number_format, c.font = gfmt, TD
            for i in range(lag, len(keys)):
                if partial and agg == "SUM" and keys[i] == cur_key:
                    continue
                c = ws.cell(r, c_yoy + i - lag, grow(f"{L(c_val + i)}{r}", f"{L(c_val + i - lag)}{r}"))
                c.number_format, c.font = gfmt, TD
            (pp_r if pct else rel_r).extend(
                [f"{L(c_chg)}{r}:{L(c_chg + len(keys) - 2)}{r}"]
                + ([f"{L(c_yoy)}{r}:{L(c_yoy + len(keys) - lag - 1)}{r}"] if len(keys) > lag else []))
            for i in range(1, len(heads) + 1):
                ws.cell(r, i).border = BOX
            r += 1
        add_growth_colours(ws, rel_r, pp_r, rel_span=0.30 if not half else 0.40)


# a swing counts as a trend change only past these sizes: (flip floor, swing) for a value metric as a
# fraction and for a percentage metric in points
_TREND_MOM = {"rel": (0.01, 0.10), "pp": (0.25, 1.0)}
_TREND_YOY = {"rel": (0.01, 0.15), "pp": (0.25, 2.0)}


def _trend_block(ws: Worksheet, arow: dict[int, int], grp_rows: list[int], c_mom: int, c_yoy: int,
                 n: int, col: int, last_row: int) -> None:
    """Two columns that say where the direction changed this month, so a reviewer can filter to them.

    MoM trend compares this month's MoM with last month's: a flip of sign is "Turned up/down", a big
    change in pace is "Accelerating/Slowing". YoY trend does the same on YoY. Both are formulas on the
    growth cells, so they follow any edit. The text carries both figures. Colour is neutral on purpose:
    for some metrics (cancellations, returns) up is not good.
    """
    from openpyxl.formatting.rule import FormulaRule
    if n < 3:
        return
    L = get_column_letter

    def fmt(x: str, pct: bool) -> str:
        return f'TEXT({x},"+0.0;-0.0")&" pp"' if pct else f'TEXT({x},"+0.0%;-0.0%")'

    def formula(a: str, b: str, pct: bool, th: dict, up: tuple[str, str], flip: tuple[str, str]) -> str:
        floor, swing = th["pp" if pct else "rel"]
        shown = f'&" ("&{fmt(b, pct)}&" to "&{fmt(a, pct)}&")"'
        return (f'=IF(AND(ISNUMBER({a}),ISNUMBER({b})),'
                f'IF(AND({a}*{b}<0,ABS({a})>={floor},ABS({b})>={floor}),IF({a}>0,"{flip[0]}","{flip[1]}"){shown},'
                f'IF({a}-{b}>={swing},"{up[0]}"{shown},IF({b}-{a}>={swing},"{up[1]}"{shown},""))),"")')

    heads = [(col, "MoM trend"), (col + 1, "YoY trend")]
    c = ws.cell(4, col, "Trend change this month")
    c.font, c.fill, c.alignment = TH, PatternFill("solid", fgColor="2F5597"), Alignment(horizontal="center")
    ws.merge_cells(start_row=4, start_column=col, end_row=4, end_column=col + 1)
    for cc, h in heads:
        c = ws.cell(5, cc, h)
        c.font, c.fill = TH, FILL_HEAD
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[L(cc)].width = 34
    for rr in grp_rows:
        for cc in (col, col + 1):
            ws.cell(rr, cc).fill = FILL_GROUP
    ma, mb = L(c_mom + n - 2), L(c_mom + n - 3)
    ya, yb = L(c_yoy + n - 1), L(c_yoy + n - 2)
    for r in arow.values():
        pct = ws.cell(r, 4).value == "%"
        for cc, f_ in ((col, formula(f"{ma}{r}", f"{mb}{r}", pct, _TREND_MOM,
                                     ("Accelerating", "Slowing"), ("Turned up", "Turned down"))),
                       (col + 1, formula(f"{ya}{r}", f"{yb}{r}", pct, _TREND_YOY,
                                         ("YoY improving", "YoY weakening"),
                                         ("YoY turned positive", "YoY turned negative")))):
            c = ws.cell(r, cc, f_)
            c.font, c.border = TD, BOX
    amber = PatternFill("solid", bgColor="FFE699")
    rng = f"{L(col)}6:{L(col + 1)}{last_row}"
    ws.conditional_formatting.add(rng, FormulaRule(formula=[f'LEN({L(col)}6)>0'], fill=amber,
                                                   font=Font(bold=True, color="7F4F00")))
    m_rng, y_rng = f"{L(col)}6:{L(col)}{last_row}", f"{L(col + 1)}6:{L(col + 1)}{last_row}"
    ws.cell(3, 1, f'="Trend changes this month: "&COUNTIF({m_rng},"?*")&" metrics on MoM, "&COUNTIF({y_rng},"?*")'
                  f'&" on YoY. Filter the MoM trend and YoY trend columns to review them."').font = H2
    ws.auto_filter.ref = f"A5:{L(col + 1)}{last_row}"


def _period_blocks(ws: Worksheet, cfg: dict, month: str, months: list[str], col_of: dict[str, int],
                   layout: list[dict], arow: dict[int, int], grp_rows: list[int], rec: list[str],
                   c_rec: int, c_start: int, last_row: int) -> None:
    """Quarterly and half-yearly values, growth and YoY, to the right of the monthly view.

    So months, quarters and half-years can be read on one sheet. Months inside the monthly
    window point at the cells on this sheet (edit one and the quarter follows); earlier months
    point at the History sheet. Sums and averages follow `_agg_of`.
    """
    L = get_column_letter
    hrow = {item["par"]: item["row"] for item in layout}
    rec_at = {m: c_rec + i for i, m in enumerate(rec)}
    cur_blk = PatternFill("solid", fgColor="2F5597")
    col = c_start
    ends = col
    for half, title, keep, lag, chg in ((False, "Quarterly", 8, 4, "QoQ"), (True, "Half-yearly", 4, 2, "HoH")):
        span = 6 if half else 3
        cur_key = _fy_key(month, half)
        all_keys = sorted({_fy_key(m, half) for m in months})
        full = {k: [m for m in months if _fy_key(m, half) == k] for k in all_keys}
        keys = [k for k in all_keys if len(full[k]) == span or k == cur_key][-keep:]
        if not keys:
            continue
        partial = len(full[cur_key]) < span
        n = len(keys)
        c_val, c_chg = col, col + n + 1
        c_yoy = c_chg + max(n - 1, 0) + 1
        spec = [(c_val, n, f"{title}: values"), (c_chg, n - 1, f"{chg} growth"), (c_yoy, n - lag, "YoY growth")]
        for c0, w, text in spec:
            if w <= 0:
                continue
            c = ws.cell(4, c0, text)
            c.font, c.fill, c.alignment = TH, cur_blk, Alignment(horizontal="center")
            if w > 1:
                ws.merge_cells(start_row=4, start_column=c0, end_row=4, end_column=c0 + w - 1)
        labels = {c_val + i: _fy_label(k, half) + (" (to date)" if k == cur_key and partial else "")
                  for i, k in enumerate(keys)}
        labels |= {c_chg + i - 1: f"{_fy_label(keys[i], half)} vs {_fy_label(keys[i - 1], half)}"
                   for i in range(1, n)}
        labels |= {c_yoy + i - lag: f"{_fy_label(keys[i], half)} vs {_fy_label(keys[i - lag], half)}"
                   for i in range(lag, n)}
        top = c_yoy + max(n - lag, 0) - 1 if n > lag else c_chg + max(n - 1, 0) - 1
        for c0 in range(c_val, top + 1):
            c = ws.cell(5, c0, labels.get(c0, ""))
            c.font, c.fill = TH, FILL_HEAD
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws.column_dimensions[L(c0)].width = 12 if c0 in labels else 2
        for rr in grp_rows:
            for c0 in range(c_val, top + 1):
                ws.cell(rr, c0).fill = FILL_GROUP

        rel_r: list[str] = []
        pp_r: list[str] = []
        for item in layout:
            par = item["par"]
            r = arow.get(par)
            if r is None:
                continue
            pct = item["unit"] == "%"
            agg = _agg_of(item["name"], item["unit"])
            vfmt = "0.00%" if pct else ("#,##0.00" if item["unit"] in ("INR Cr", "Mn", "#", "") else "#,##0")
            gfmt = '0.0" pp"' if pct else "0.0%"
            for i, k in enumerate(keys):
                refs = ",".join(f"{L(rec_at[m])}{r}" if m in rec_at else f"History!{L(col_of[m])}{hrow[par]}"
                                for m in full[k])
                c = ws.cell(r, c_val + i, f'=IF(COUNT({refs})<{len(full[k])},"",{agg}({refs}))')
                c.number_format, c.font = vfmt, (TD_B if k == cur_key else TD)
            grow = (lambda a, b: f'=IF(AND(ISNUMBER({a}),ISNUMBER({b})),({a}-{b})*100,"")') if pct else \
                   (lambda a, b: f'=IF(AND(ISNUMBER({a}),ISNUMBER({b}),{b}<>0),{a}/{b}-1,"")')
            for i in range(1, n):
                if partial and agg == "SUM" and keys[i] == cur_key:
                    continue            # a part-period total against a whole one says nothing
                c = ws.cell(r, c_chg + i - 1, grow(f"{L(c_val + i)}{r}", f"{L(c_val + i - 1)}{r}"))
                c.number_format, c.font = gfmt, TD
            for i in range(lag, n):
                if partial and agg == "SUM" and keys[i] == cur_key:
                    continue
                c = ws.cell(r, c_yoy + i - lag, grow(f"{L(c_val + i)}{r}", f"{L(c_val + i - lag)}{r}"))
                c.number_format, c.font = gfmt, TD
            for c0 in range(c_val, top + 1):
                ws.cell(r, c0).border = BOX
            if n > 1:
                (pp_r if pct else rel_r).append(f"{L(c_chg)}{r}:{L(c_chg + n - 2)}{r}")
            if n > lag:
                (pp_r if pct else rel_r).append(f"{L(c_yoy)}{r}:{L(c_yoy + n - lag - 1)}{r}")
        add_growth_colours(ws, rel_r, pp_r, rel_span=0.40 if half else 0.30)
        col = top + 2
        ends = top
    if ends > c_start:
        ws.cell(3, c_start, "Quarterly and half-yearly views: fiscal year April to March; GMV and order counts "
                            "are summed, rates and levels averaged.").font = H2


RUN_DATA = "Run data"


def _run_data_sheet(wb: Workbook, ctx: dict[str, Any]) -> None:
    """A hidden copy of this month's figures as the run produced them.

    When the Analysis workbook comes back edited, this is what the edits are measured against, so
    a figure someone changed is never passed off as the MIS or the model's own.
    """
    ref, est, month, cfg = ctx["reference"], ctx["estimate"], ctx["month"], ctx["config"]
    ws = wb.create_sheet(RUN_DATA)
    for i, h in enumerate(["par_id", "value", "provenance", "confidence", "how it was produced", "month"], 1):
        ws.cell(1, i, h)
    r = 2
    for t, sec in analysis_template(ref, est, cfg):
        v = est.value_for_par(ref, t.par_id)
        if v is None or v.is_null:
            continue
        for i, x in enumerate([t.par_id, v.value, v.provenance.value, v.confidence.value, v.method, month], 1):
            ws.cell(r, i, x)
        r += 1
    ws.sheet_state = "hidden"
