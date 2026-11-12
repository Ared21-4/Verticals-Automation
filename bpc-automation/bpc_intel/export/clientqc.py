"""QC a client sheet against our Output.

The client sheet has months across, a Parameter and a Units column, and no par_ids; its labels and
section names differ from ours. So rows are matched by their *values*: the client sheet carries years of
history and so does our reference, and a row whose past months line up with one of ours is the same
metric whatever it is called. The mapping is saved per player and reused every month; unsure rows can be
corrected in the portal. Nothing here changes any Output or Analysis file.
"""
from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import numpy as np
import openpyxl

MAPS = Path(__file__).resolve().parent.parent / "config" / "client_maps"
_MON = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
SCALES = (1.0, 100.0, 0.01)
OUT_PCT_POINTS = True        # the Output keeps percentages in points (Fashion, BPC); False where it keeps fractions


def norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower().replace("\xa0", " ")).strip()


def month_key(h: Any) -> str | None:
    """"Aug'2026", "Aug'26", "Aug-26", a date -> "2026-08"."""
    if hasattr(h, "year") and hasattr(h, "month"):
        return f"{h.year:04d}-{h.month:02d}"
    m = re.match(r"\s*([A-Za-z]{3})[a-z]*\W*(\d{2,4})", str(h or ""))
    if not m or m.group(1).lower() not in _MON:
        return None
    y = int(m.group(2))
    return f"{y + 2000 if y < 100 else y:04d}-{_MON[m.group(1).lower()]:02d}"


def num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if np.isfinite(v) else None
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


# ------------------------------------------------------------------ the client sheet

def client_sheets(path) -> list[str]:
    """Sheets that look like a player's raw data (a Parameter column and month headers), formatted copies last."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        head = next(ws.iter_rows(min_row=1, max_row=8, values_only=True), None)
        months = 0
        for row in ws.iter_rows(min_row=1, max_row=8, values_only=True):
            months = max(months, sum(1 for h in row if month_key(h)))
        if months >= 6:
            out.append(ws.title)
    wb.close()
    return sorted(out, key=lambda t: (t.lower().endswith("_formatted"), t))


def read_client(path, sheet: str) -> dict[str, Any]:
    """Rows of one client sheet: [{row, parent, name, unit, series{month: value}}] and its month list."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = [list(r) for r in wb[sheet].iter_rows(values_only=True)]
    wb.close()
    hr = next(i for i, r in enumerate(rows[:12]) if sum(1 for h in r if month_key(h)) >= 6)
    head = rows[hr]
    mcols = {c: month_key(h) for c, h in enumerate(head) if month_key(h)}
    first = min(mcols)
    name_c = next((c for c in range(first) if norm(head[c]) in ("parameter", "parameters", "metric")), first - 2)
    unit_c = next((c for c in range(first) if norm(head[c]) in ("unit", "units")), first - 1)
    out, parent = [], None
    for i, r in enumerate(rows[hr + 1:], hr + 2):
        nm = r[name_c] if name_c < len(r) else None
        if nm is None or not str(nm).strip():
            continue
        series = {m: v for c, m in mcols.items() if c < len(r) and (v := num(r[c])) is not None}
        unit = str(r[unit_c]).replace("\xa0", " ").strip() if unit_c < len(r) and r[unit_c] else ""
        if not unit and not series:
            parent = str(nm).strip()
            continue
        out.append({"row": i, "parent": parent or "", "name": str(nm).strip(), "unit": unit, "series": series})
    return {"rows": out, "months": sorted(set(mcols.values())), "sheet": sheet}


# ------------------------------------------------------------------ our side

def our_history(ref, cfg) -> dict[int, dict[str, Any]]:
    """par_id -> {name, unit, series{month: value}} from the player's reference (and derived history)."""
    from .workbook import derived_history
    der = derived_history(ref, cfg)
    out: dict[int, dict[str, Any]] = {}
    for par, row in ref.par_to_row.items():
        s = {m: v for m in ref.months if (v := ref.value(row, m)) is not None}
        for m, v in (der.get(par) or {}).items():
            s.setdefault(m, v)
        if s:
            out[par] = {"name": str(ref.row_names.get(row, "")), "unit": str((getattr(ref, "row_units", {}) or {}).get(row, "")),
                        "series": s}
    for par, d in der.items():
        if par not in out and d:
            out[par] = {"name": f"par {par}", "unit": "", "series": dict(d)}
    return out


# ------------------------------------------------------------------ matching

def _matrix(rows: list[dict], months: list[str]) -> np.ndarray:
    ix = {m: i for i, m in enumerate(months)}
    a = np.full((len(rows), len(months)), np.nan)
    for r, d in enumerate(rows):
        for m, v in d["series"].items():
            if m in ix:
                a[r, ix[m]] = v
    return a


def match_rows(client_rows: list[dict], ours: dict[int, dict], min_overlap: int = 6,
               extra_names: dict[int, str] | None = None) -> list[dict]:
    """One mapping per client row: {row, par_id, scale, score, how}. Value fingerprints first, then names."""
    pars = list(ours)
    months = sorted({m for d in client_rows for m in d["series"]} & {m for p in pars for m in ours[p]["series"]})
    C, O = _matrix(client_rows, months), _matrix([ours[p] for p in pars], months)
    names = [norm(ours[p]["name"]) for p in pars]
    cand: list[tuple[float, int, int, float, float]] = []
    for i, d in enumerate(client_rows):
        c = C[i]
        okc = ~np.isnan(c)
        if okc.sum() < min_overlap:
            continue
        cn = norm(d["name"])
        for j in range(len(pars)):
            both = okc & ~np.isnan(O[j])
            n = int(both.sum())
            if n < min_overlap:
                continue
            best_s, best_sc = 1.0, -1.0
            for s in SCALES:
                x, y = c[both], O[j][both] * s
                tol = 0.005 * np.maximum(np.abs(x), np.abs(y)) + 1e-6
                sc = float((np.abs(x - y) <= tol).mean())
                if sc > best_sc:
                    best_s, best_sc = s, sc
            if best_sc < 0.6:
                continue
            flat = np.nanstd(c[both]) < 1e-9
            sim = SequenceMatcher(None, cn, names[j]).ratio()
            rank = best_sc * (0.5 if flat else 1.0) + 0.35 * sim + 0.05 * min(n, 60) / 60
            cand.append((rank, i, j, best_s, best_sc))
    cand.sort(reverse=True)
    taken_c, taken_o, res = set(), set(), {}
    for rank, i, j, s, sc in cand:
        if i in taken_c or j in taken_o:
            continue
        taken_c.add(i)
        if sc < 0.95:                       # a sure match may serve duplicate client rows (the same series repeated)
            taken_o.add(j)
        res[i] = {"par_id": pars[j], "scale": s, "score": round(sc, 3), "how": "values"}
    # rows the history cannot place (no overlap): a unique name match among our rows and the webform's names
    used = {m["par_id"] for m in res.values() if m["par_id"] is not None}
    pool: dict[int, str] = {p: ours[p]["name"] for p in pars}
    for p, n in (extra_names or {}).items():
        pool.setdefault(p, n)
    by_name: dict[str, list[int]] = {}
    for p, n in pool.items():
        if p not in used:
            by_name.setdefault(norm(n), []).append(p)
    for i, d in enumerate(client_rows):
        if i in res:
            continue
        ps = by_name.get(norm(d["name"]), [])
        if len(ps) == 1:
            res[i] = {"par_id": ps[0], "scale": 100.0 if "%" in d["unit"] else 1.0, "score": 0.0, "how": "name"}
            by_name[norm(d["name"])] = []
    out = []
    for i, d in enumerate(client_rows):
        m = res.get(i)
        out.append({"row": d["row"], "parent": d["parent"], "name": d["name"], "unit": d["unit"],
                    "par_id": m["par_id"] if m else None, "scale": m["scale"] if m else None,
                    "score": m["score"] if m else None, "how": m["how"] if m else "none"})
    return out


def confidence(m: dict) -> str:
    if m["par_id"] is None:
        return "unmatched"
    if m["how"] == "values":
        return "high" if m["score"] >= 0.95 else "medium"
    return "name only"


# ------------------------------------------------------------------ saved mappings

def _map_path(player: str) -> Path:
    return MAPS / (re.sub(r"[^a-z0-9]+", "", player.lower()) + ".json")


def load_overrides(player: str) -> dict[str, Any]:
    p = _map_path(player)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_overrides(player: str, data: dict[str, Any]) -> None:
    MAPS.mkdir(parents=True, exist_ok=True)
    _map_path(player).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8", newline="")


def key_of(m: dict) -> str:
    return f"{norm(m['parent'])}|{norm(m['name'])}"


def apply_overrides(mapping: list[dict], over: dict[str, Any]) -> list[dict]:
    """A saved correction wins: {"parent|name": par_id or null}."""
    for m in mapping:
        k = key_of(m)
        if k in over:
            m["par_id"], m["how"], m["score"] = over[k], "saved", 1.0
    return mapping


# ------------------------------------------------------------------ the comparison

def prior_months(month: str, n: int) -> list[str]:
    y, m = int(month[:4]), int(month[5:7])
    out = []
    for i in range(n - 1, -1, -1):
        k = y * 12 + (m - 1) - i
        out.append(f"{k // 12:04d}-{k % 12 + 1:02d}")
    return out


def compare(client: dict, mapping: list[dict], ours: dict[int, dict], output: dict[int, float] | None,
            month: str, tol_val: float = 0.02, tol_pp: float = 0.5, window: int = 3,
            out_month: str | None = None) -> list[dict]:
    """One line per client row with figures: the client's last `window` months ending at `month`, beside ours.

    `month` is the month judged (the Output's month, or the client's latest if the client sheet stops earlier).
    The Output belongs to `out_month`; when that is later than `month` it is added as a last column, so the latest
    month always shows. Our other figures come from our reference history. Everything is in the client's units
    (percentages in points)."""
    months = prior_months(month, window) + ([out_month] if out_month and out_month > month else [])
    ci = window - 1
    cl = {d["row"]: d for d in client["rows"]}
    out = []
    for m in mapping:
        d = cl[m["row"]]
        par, sc_ = m["par_id"], (m["scale"] or 1.0)
        pct = "%" in d["unit"]
        hist = ours.get(par, {}).get("series", {}) if par is not None else {}
        c_win = [d["series"].get(x) for x in months]
        o_win, srcs = [], []
        for x in months:
            v, s_ = None, ""
            if par is not None:
                if x == out_month and output is not None and output.get(par) is not None:
                    v, s_ = float(output[par]) * ((sc_ / 100) if (pct and OUT_PCT_POINTS) else sc_), "Output"
                elif hist.get(x) is not None:
                    v, s_ = hist[x] * sc_, "reference"
            o_win.append(v)
            srcs.append(s_)
        if not any(v is not None for v in c_win):
            continue                                       # the list is the client sheet's rows that carry figures
        c, mine = c_win[ci], o_win[ci]
        diff, status = None, ""
        if par is None:
            status = "client only (unmapped)"
        elif c is None and mine is None:
            status = "none this month"
        elif c is None:
            status = "ours only"
        elif mine is None:
            status = "client only"
        else:
            diff = (c - mine) * (1.0 if sc_ == 100.0 else 100.0) if pct else ((c / mine - 1) if mine else None)
            lim = tol_pp if pct else tol_val
            status = "check" if diff is None else ("match" if abs(diff) <= lim / 10 else ("within tolerance" if abs(diff) <= lim else "off"))
        out.append({"row": m["row"], "parent": d["parent"], "name": d["name"], "unit": d["unit"], "par_id": par,
                    "confidence": confidence(m), "client": c, "ours": mine, "source": srcs[ci], "diff": diff, "status": status,
                    "months": months, "check_index": ci, "c_win": c_win, "o_win": o_win, "scale": sc_, "pct": pct})
    return out


def latest_month(client: dict, floor: float = 0.05) -> str | None:
    """The client sheet's latest month that carries figures for at least `floor` of its rows."""
    rows = [d for d in client["rows"] if d["series"]]
    count: dict[str, int] = {}
    for d in rows:
        for m in d["series"]:
            count[m] = count.get(m, 0) + 1
    ok = [m for m, n in count.items() if rows and n / len(rows) >= floor]
    return max(ok) if ok else None


# ------------------------------------------------------------------ the QC workbook

_STATUS_ORDER = {"off": 0, "client only (unmapped)": 1, "client only": 2, "ours only": 3, "check": 4,
                 "within tolerance": 5, "match": 6, "none this month": 7, "no figure either side": 8}
REVIEW = ("off", "client only (unmapped)", "client only", "ours only", "check")
STATUSES = ["off", "client only (unmapped)", "client only", "ours only", "check", "within tolerance", "match", "none this month"]


def write_qc(path, runs: list[dict], tol_val: float, tol_pp: float) -> Path:
    """One workbook for all players. Per player: the client sheet's rows, the client's last months beside ours, then the
    difference and the status as formulas (they read the tolerances on the Summary sheet, so changing a figure or a
    tolerance updates them). The Summary counts are COUNTIFs over those status columns."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.utils import get_column_letter as L

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    blue = PatternFill("solid", fgColor="2F5597")
    ws["A1"] = "Client sheet QC"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"], ws["B2"] = "Tolerance on values", tol_val
    ws["A3"], ws["B3"] = "Tolerance on percentages (points)", tol_pp
    ws["B2"].number_format, ws["B3"].number_format = "0.0%", "0.0"
    for a in ("B2", "B3"):
        ws[a].fill, ws[a].font = PatternFill("solid", fgColor="FFF2CC"), Font(bold=True, color="0000FF")
    ws["C2"] = "Change these and every status below recalculates. Percentages are compared in points."
    ws["C2"].font = Font(italic=True, color="7F7F7F")
    ws.append([])
    ws.append(["Player", "Month judged", "Our Output month", "Client tab"] + STATUSES)
    for c in ws[5]:
        c.font, c.fill = Font(bold=True, color="FFFFFF"), blue
        c.alignment = Alignment(wrap_text=True, vertical="center")
    fills = {"off": "F8CBAD", "client only (unmapped)": "FFE699", "client only": "FFE699", "ours only": "FFE699",
             "check": "FFE699", "within tolerance": "E2EFDA", "match": "C6E0B4"}
    grey = PatternFill("solid", fgColor="F2F2F2")
    used: set[str] = set()
    for run in runs:
        res = run["results"]
        title = re.sub(r"[\\/*?:\[\]]", "", run["player"])[:28] or "Player"
        while title in used:
            title = title[:26] + "_2"
        used.add(title)
        sh = wb.create_sheet(title)
        months = res[0]["months"] if res else prior_months(run["month"], 3)
        n, ci = len(months), (res[0]["check_index"] if res else 2)
        c0, o0 = 5, 5 + n                                  # first client column, first ours column
        dcol, scol = 5 + 2 * n, 6 + 2 * n
        head = ["Section", "Parameter", "Units", "par_id"] + [f"Client {m}" for m in months] \
            + [f"Ours {m}" + (" (Output)" if m == run.get("out_month") else "") for m in months] \
            + [f"Difference {run['month']}", "Status"]
        sh.append(head)
        for c in sh[1]:
            c.font, c.fill = Font(bold=True, color="FFFFFF"), blue
            c.alignment = Alignment(wrap_text=True, vertical="center")
        for c in range(o0, o0 + n):
            sh.cell(1, c).fill = PatternFill("solid", fgColor="548235")
        last = None
        for x in res:
            sec = x["parent"] if x["parent"] != last else ""
            last = x["parent"]
            sh.append([sec, x["name"], x["unit"], x["par_id"]] + x["c_win"] + x["o_win"])
            r = sh.max_row
            cc, oc = f"{L(c0 + ci)}{r}", f"{L(o0 + ci)}{r}"
            pts = 1 if x["scale"] == 100.0 else 100
            if x["pct"]:
                sh.cell(r, dcol, f'=IF(AND(ISNUMBER({cc}),ISNUMBER({oc})),({cc}-{oc})*{pts},"")').number_format = '0.0" pp"'
                lim = "Summary!$B$3"
            else:
                sh.cell(r, dcol, f'=IF(AND(ISNUMBER({cc}),ISNUMBER({oc}),{oc}<>0),{cc}/{oc}-1,"")').number_format = "0.0%"
                lim = "Summary!$B$2"
            d = f"{L(dcol)}{r}"
            sh.cell(r, scol, (f'=IF($D{r}="","client only (unmapped)",IF(NOT(ISNUMBER({cc})),IF(ISNUMBER({oc}),"ours only","none this month"),'
                              f'IF(NOT(ISNUMBER({oc})),"client only",IF(NOT(ISNUMBER({d})),"check",'
                              f'IF(ABS({d})<={lim}/10,"match",IF(ABS({d})<={lim},"within tolerance","off"))))))'))
            for c in range(c0, o0 + n):
                sh.cell(r, c).number_format = "#,##0.00"
            for c in range(o0, o0 + n):
                sh.cell(r, c).fill = grey
        end = max(sh.max_row, 2)
        rng = f"{L(scol)}2:{L(scol)}{end}"
        for st_, color in fills.items():
            sh.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=[f'"{st_}"'],
                                                          fill=PatternFill("solid", bgColor=color, fgColor=color)))
        for i, wd in enumerate([22, 34, 9, 8] + [11] * (2 * n) + [13, 22], 1):
            sh.column_dimensions[L(i)].width = wd
        sh.row_dimensions[1].height = 30
        sh.freeze_panes = sh.cell(2, 5)
        sh.auto_filter.ref = f"A1:{L(scol)}{end}"
        q = "'" + title.replace("'", "''") + "'"
        ws.append([run["player"], run["month"], run.get("out_month") or "", run["sheet"]]
                  + [f'=COUNTIF({q}!${L(scol)}$2:${L(scol)}${end},"{st_}")' for st_ in STATUSES])
    for i, wd in enumerate([22, 14, 14, 20] + [12] * len(STATUSES), 1):
        ws.column_dimensions[L(i)].width = wd
    ws.row_dimensions[5].height = 32
    wb.save(path)
    return Path(path)


# ------------------------------------------------------------------ one player, start to finish

def run_player(player: str, client: dict, ref_path, output: dict[int, float] | None, month: str,
               extra_names: dict[int, str] | None = None, tol_val: float = 0.02, tol_pp: float = 0.5) -> dict:
    """Match one player's client rows to ours (saved corrections win) and compare.

    `month` is the Output's month. It is the month judged when the client sheet has figures for it; if the client sheet
    stops earlier, the client's latest month is judged and the Output's month still shows as a last column."""
    from ..config_resolver import resolve_config
    from ..readers.reference import load_reference

    cfg = resolve_config(player, reference_path=ref_path)
    ref = load_reference(ref_path, cfg)
    ours = our_history(ref, cfg)
    mapping = apply_overrides(match_rows(client["rows"], ours, extra_names=extra_names), load_overrides(player))
    latest = latest_month(client)
    judged = month if (latest and month <= latest) else (latest or month)
    results = compare(client, mapping, ours, output, judged, tol_val=tol_val, tol_pp=tol_pp, out_month=month)
    return {"player": player, "month": judged, "out_month": month, "sheet": client["sheet"], "client": client, "ours": ours,
            "mapping": mapping, "results": results, "extra": extra_names or {}, "output": output}


def tab_for(player: str, sheets: list[str]) -> str | None:
    """The client's raw tab for a player: its name without spaces/punctuation appears in the tab name."""
    key = norm(player).replace(" ", "")
    raw = [t for t in sheets if not t.lower().endswith("_formatted")]
    return next((t for t in raw if key and (key in norm(t).replace(" ", "") or norm(t).replace(" ", "") in key)), None)


# ------------------------------------------------------------------ our figures: an Output or an Analysis workbook

def read_figures(path) -> dict[str, Any]:
    """{kind, player, label, month, values{par_id: figure}} from an Output or an Analysis workbook.

    An Analysis file gives this month's column (a figure edited and saved in Excel is used as it stands; where a cell
    holds a formula with no stored result, the run's own figure from the hidden "Run data" sheet stands in). Percentages
    come back the way the Output keeps them, so the two kinds are interchangeable."""
    wb = openpyxl.load_workbook(path, data_only=True)
    try:
        if "Analysis" in wb.sheetnames:
            ws = wb["Analysis"]
            player, _, rest = str(ws["A1"].value or "").partition(":")
            m = re.match(r"\s*([A-Za-z]{3,9}\W*\d{2,4})", rest)
            label = m.group(1).strip() if m else ""
            mk = month_key(label)
            head = [c.value for c in ws[5]]
            cur = next((i for i, h in enumerate(head) if i >= 4 and mk and month_key(h) == mk and " vs " not in str(h)), None)
            if cur is None:
                raise ValueError("this Analysis workbook has no column for its own month")
            run: dict[int, float] = {}
            if "Run data" in wb.sheetnames:
                for r in wb["Run data"].iter_rows(min_row=2, values_only=True):
                    if isinstance(r[0], int) and num(r[1]) is not None:
                        run.setdefault(r[0], num(r[1]))
            vals: dict[int, float] = {}
            for r in ws.iter_rows(min_row=6, values_only=True):
                if not isinstance(r[0], int) or cur >= len(r):
                    continue
                v = num(r[cur])
                if v is None:
                    v = run.get(r[0])
                if v is None:
                    continue
                vals[r[0]] = v * 100 if ("%" in str(r[3] or "") and OUT_PCT_POINTS) else v
            return {"kind": "Analysis", "player": player.strip(), "label": label, "month": mk, "values": vals}
        from .webform import output_label, output_values
        pl, mo = output_label(path)
        return {"kind": "Output", "player": pl, "label": mo, "month": month_key(mo.replace(" ", "'")),
                "values": {k: float(v) for k, v in output_values(path).items()}}
    finally:
        wb.close()
