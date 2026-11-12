"""Furniture Player Intelligence portal.

A thin interface over `furniture_intel`. Every calculation lives in the engine, so the same run
can be reproduced from the CLI or a test.

    streamlit run app.py
"""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from pathlib import Path

import pandas as pd
import streamlit as st

from furniture_intel.batch import default_batch, match_player, run_batch, zip_name
from furniture_intel.domain import Provenance, Severity, month_label, shift_months
from furniture_intel.history import calibration_records
from furniture_intel.history import describe as history_describe
from furniture_intel.history import remove_month
from furniture_intel.pipeline import (commit_history, list_players, load_default_weights,
                                    load_player_config, run_month)
from furniture_intel.reference_store import build_slim_reference, default_reference
from furniture_intel.sniff import sniff_file
from furniture_intel.store import Override, Store
from furniture_intel.trend import reference_months, trend_rows
from portal_theme import css, icon

st.set_page_config(page_title="Furniture Player Intelligence", page_icon="🛋️",
                   layout="wide", initial_sidebar_state="expanded")

STORE = Store("data")
MUTED = "#6B6B6B"
DARK = bool(st.session_state.get("dark_mode", False))
st.markdown(css(DARK), unsafe_allow_html=True)

PROV_LABEL = {"reported": "Reported", "derived": "Derived", "estimated": "Estimated",
              "assumed": "Assumed", "unavailable": "Unavailable", "calibrated": "Calibrated",
              "call_stated": "Call"}
SEV_ICON = {"error": "🔴 error", "warning": "🟠 warning", "info": "🟢 info"}
ALL_PLAYERS = "★ All selected players"
SHAREABLE = ("traffic", "logistics", "social", "brand_level", "secondary")
PLAYER_SPECIFIC = ("mis", "transcript", "prior_call")
KINDS = {"reference": "Reference workbook", "mis": "Player MIS", "transcript": "Expert call (.docx)",
         "traffic": "Traffic data", "logistics": "Logistics data", "social": "Social media data",
         "brand_level": "Brand-level data", "secondary": "Secondary data (news)",
         "prior_call": "Last known call (earlier month)", "other": "Other"}


# ------------------------------------------------------------------ small helpers

def tag(prov: str) -> str:
    return f'<span class="tag t-{prov}">{PROV_LABEL.get(prov, prov)}</span>'


def tile(label: str, value: str, prov: str | None = None, note: str = "") -> str:
    t = tag(prov) if prov else ""
    n = f"<span>{note}</span>" if note else ""
    return (f'<div class="tile"><div class="l">{label}</div><div class="v">{value}</div>'
            f'<div class="s">{t}{n}</div></div>')


def tiles(items: list[str], tight: bool = False) -> None:
    st.markdown(f'<div class="tiles{" tight" if tight else ""}">{"".join(items)}</div>',
                unsafe_allow_html=True)


def section(title: str, sub: str = "") -> None:
    st.markdown(f'<div class="sec">{title}</div>' + (f'<div class="sub">{sub}</div>' if sub else ""),
                unsafe_allow_html=True)


def status_row(label: str, state: str, text: str, name: str = "") -> str:
    nm = f'<span class="name" title="{name}">{name}</span>' if name else ""
    return f'<div class="row"><span>{label}</span>{nm}<span class="pill {state}">{text}</span></div>'


def _persist(upload) -> str:
    d = Path(tempfile.mkdtemp(prefix="fpi_"))
    p = d / upload.name
    p.write_bytes(upload.getbuffer())
    return str(p)


def _today_month() -> str:
    import datetime as dt
    t = dt.date.today()
    return f"{t.year:04d}-{t.month:02d}"


def _month_options(anchor: str | None = None, n_back: int = 36, n_fwd: int = 24) -> list[str]:
    """Newest first. Runs `n_fwd` months past the later of today and `anchor` (the end of the
    player's history, or its last saved month), so the list keeps extending as time and data move on."""
    base = max(_today_month(), anchor or "")
    return [shift_months(base, i) for i in range(n_fwd, -n_back, -1)]


def _history_sig() -> tuple:
    d = Path("data/history")
    return tuple(sorted((p.name, p.stat().st_mtime) for p in d.glob("*.json"))) if d.exists() else ()


@st.cache_data(show_spinner=False)
def _players(ref_path: str | None, sig: tuple):
    return list_players(ref_path)


@st.cache_data(show_spinner=False)
def _sniff_cached(name: str, size: int, _path: str) -> str:
    return sniff_file(_path)


def _fmt(v: float | None, unit: str = "") -> str:
    if v is None:
        return "n/a"
    if unit == "%":
        return f"{v * 100:,.1f}%"
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.1f}"


def _pct(x: float | None, pp: bool = False) -> str:
    if x is None:
        return ""
    return f"{x * 100:+.1f} pts" if pp else f"{x:+.1%}"


def _h(n: int, cap: int = 320) -> int:
    return min(38 + 35 * max(n, 1), cap)


def _tint(span: float):
    """Cell colour for a figure, red to yellow to green: red when low or falling, yellow near zero."""
    red, yellow, green = (248, 105, 107), (255, 235, 132), (99, 190, 123)

    def f(x):
        if x is None or pd.isna(x):
            return ""
        t = max(-1.0, min(1.0, x / span))
        a, b, w = (red, yellow, t + 1) if t < 0 else (yellow, green, t)
        rgb = tuple(round(a[i] + (b[i] - a[i]) * w) for i in range(3))
        return f"background-color: rgb({rgb[0]},{rgb[1]},{rgb[2]}); color: #1A1A1A"
    return f


def _tinted(df: pd.DataFrame, cols: list[str], span: float = 30.0, fmt: str = "{:+.1f}"):
    cols = [c for c in cols if c in df.columns]
    return df.style.map(_tint(span), subset=cols).format(fmt, subset=cols, na_rep="")


def _stretch_df(df, cap: int = 320, **cfg):
    n = len(df.data) if hasattr(df, "data") else len(df)
    st.dataframe(df, hide_index=True, width="stretch", height=_h(n, cap), column_config=cfg or None)

# ------------------------------------------------------------------ sidebar

BUILTIN_PATH = default_reference()
rows = _players(BUILTIN_PATH, _history_sig())
by_key = {r["key"]: r for r in rows}
by_name = {r["name"]: r for r in rows}
options = [r["key"] for r in rows]
if not options:
    st.error("The built-in reference is missing. Upload the master workbook on the Upload tab.")
    st.stop()

with st.sidebar:
    st.markdown(f'<div class="brand"><div class="logo">{icon("sofa", "white", 22)}</div><div><b>Furniture Intelligence</b>'
                '<span>Furniture &amp; home</span></div></div>',
                unsafe_allow_html=True)
    mode = st.radio("Mode", ["One player", "Several players"], horizontal=True,
                    label_visibility="collapsed", key="mode")
    BATCH = mode == "Several players"

    if BATCH:
        player, _p = None, {}
        latests = Counter(by_name[n]["latest_month"] for n in default_batch() if n in by_name)
        latest = latests.most_common(1)[0][0] if latests else None
        saved = []
        st.caption("Choose the players on the Upload tab.")
    else:
        st.markdown("### Player")
        player = st.selectbox("Player", options, label_visibility="collapsed",
                              index=options.index("pepperfry") if "pepperfry" in options else 0,
                              format_func=lambda k: by_key[k]["name"])
        _p = by_key[player]
        latest, saved = _p.get("latest_month"), _p.get("saved_months") or []

    st.markdown("### Reporting month")
    months = _month_options(max([m for m in [latest, *saved] if m], default=None))
    default_m = shift_months(saved[-1], 1) if saved else (latest or _today_month())
    default_m = default_m if default_m in months else _today_month()
    month = st.selectbox("Month", months, index=months.index(default_m),
                         key=f"month_{player or 'batch'}", label_visibility="collapsed",
                         format_func=month_label)
    st.caption(f"History runs to **{month_label(latest)}**" if latest else "")

    st.markdown("### History weight")
    hw = st.slider("History weight", 0, 100, 100, 5, format="%d%%", key=f"hw_{player or 'batch'}",
                   label_visibility="collapsed",
                   help="How far the output moves from the MIS figure toward the level that keeps "
                        "MoM and YoY consistent with the stored history. 0% = the MIS unchanged.")

    st.toggle("Dark mode", key="dark_mode")

    with st.expander("More settings"):
        seasonality = st.radio(
            "Calendar seasonality", ["auto", "bau", "festive"],
            format_func={"auto": "Auto, follow the call", "bau": "Business as usual",
                         "festive": "Festive uplift"}.get)
        saved_w = STORE.list_weights(player) if player else []
        chosen_saved = st.selectbox("Saved weights", ["(defaults)"] + [s["_name"] for s in saved_w])


def _weights_now() -> dict:
    base = load_default_weights()
    auto = next((s for s in saved_w if s.get("_player") == player), None)
    if chosen_saved != "(defaults)":
        m = next((s for s in saved_w if s["_name"] == chosen_saved), None)
        if m:
            return STORE.load_weights(m["_path"])
    elif auto:
        return STORE.load_weights(auto["_path"])
    return base


# ------------------------------------------------------------------ header

paths: dict[str, str] = st.session_state.setdefault("paths", {})
_names: dict[str, str] = st.session_state.setdefault("names", {})
hero_slot = st.empty()


def _hero(files: dict[str, str]) -> None:
    if BATCH:
        chips = ['<span class="chip">Several players</span>', f'<span class="chip">{month_label(month)}</span>']
    else:
        chips = [f'<span class="chip">{_p["name"]}</span>', f'<span class="chip">{month_label(month)}</span>']
        chips.append('<span class="chip ok">Your reference</span>' if files.get("reference")
                     else f'<span class="chip ok">Built-in history to {month_label(latest)}</span>')
        if not files.get("mis") and not files.get("transcript"):
            chips.append('<span class="chip warn">History only</span>')
    hero_slot.markdown(
        f'<div class="hero"><div><h1>Furniture Player Intelligence</h1>'
        f"<p>Furniture &amp; home: upload the month's files, review, download.</p></div>"
        f'<div class="chips">{"".join(chips)}</div></div>', unsafe_allow_html=True)


_hero(paths)

# Nine tools in four groups: the monthly run (steps 1-4), history, the deliverables, and client QC.
g_run, g_hist, g_out, tab_qc = st.tabs(["Monthly run", "History & collate", "Deliverables", "Client QC"])
with g_run:
    tab_upload, tab_weights, tab_run, tab_review = st.tabs(
        ["1 · Upload", "2 · Weights", "3 · Run all" if BATCH else "3 · Validate & run",
         "4 · Results" if BATCH else "4 · Review"])
with g_hist:
    tab_history, tab_collate = st.tabs(["History", "Collate"])
with g_out:
    tab_rebuild, tab_webform = st.tabs(["Edited Analysis", "Webform"])


# ================================================================= 1. UPLOAD

def _stage_files(files) -> tuple[dict, dict, list[str]]:
    staged, names, dupes = {}, {}, []
    for f in files or []:
        path = _persist(f)
        low = f.name.lower()
        guess = _sniff_cached(f.name, len(f.getbuffer()), path)
        if guess == "other":
            guess = ("traffic" if "traffic" in low else
                     "logistics" if ("logistic" in low or "shipped" in low) else
                     "social" if "social" in low else
                     "brand_level" if "brand" in low else "other")
        c1, c2, c3 = st.columns([4, 3.4, 1.2])
        c1.markdown(f"**{f.name}**")
        kind = c2.selectbox("type", list(KINDS), key=f"kind_{f.name}", index=list(KINDS).index(guess),
                            format_func=KINDS.get, label_visibility="collapsed")
        c3.caption(f"{len(f.getbuffer()) / 1024:,.0f} KB")
        if kind in staged and kind != "other":
            dupes.append(f"Two files are marked {KINDS[kind]}; using {f.name}.")
        staged[kind], names[kind] = path, f.name
    return staged, names, dupes


def _other_player_warnings(names: dict[str, str]) -> list[str]:
    mine = _p["name"].lower().split()[0]
    others = {r["name"].lower().split()[0]: r["name"] for r in rows if r["key"] != player}
    out = []
    for nm in names.values():
        low = nm.lower()
        for tok, full in others.items():
            if len(tok) >= 3 and tok in low and mine not in low:
                out.append(f"'{nm}' looks like {full}'s file, but {_p['name']} is selected.")
    return out


with tab_upload:
    if not BATCH:
        left, right = st.columns([3, 2], gap="large")
        names: dict[str, str] = {}
        with left, st.container(border=True):
            section("Files", "Reference workbook, MIS, expert call and any supporting data. "
                             "Each file's type is read from its contents. All optional.")
            files = st.file_uploader("Files", accept_multiple_files=True,
                                     type=["xlsx", "xlsm", "xls", "docx", "csv", "txt"],
                                     label_visibility="collapsed")
            staged, names, dupes = _stage_files(files)
            for d in dupes:
                st.warning(d)
            if staged.get("prior_call"):
                from furniture_intel.domain import shift_months as _sm
                from furniture_intel.pipeline import infer_call_month as _icm
                opts = [_sm(month, -i) for i in range(1, 13)]
                guess = _icm(staged["prior_call"]) or opts[0]
                st.session_state.prior_call_month = st.selectbox(
                    "Which month is the last known call from?", opts,
                    index=opts.index(guess) if guess in opts else 0, format_func=month_label,
                    help="Only used when there is no call for the reporting month.")
                if staged.get("transcript"):
                    st.caption("A call for this month is also uploaded, so the older call will not be used.")
            with st.expander("Advanced"):
                d1, d2 = st.columns(2)
                dm = d1.text_input("MIS file on disk", key=f"disk_mis_{player}", value="")
                dt_ = d2.text_input("Call file on disk", key=f"disk_transcript_{player}", value="")
                for key, pth in (("mis", dm), ("transcript", dt_)):
                    if pth and Path(pth).exists() and key not in staged:
                        if key == "mis" and sniff_file(pth) != "mis":
                            st.warning(f"'{pth}' does not look like a MIS, so it was not used.")
                            continue
                        staged[key], names[key] = pth, Path(pth).name
                st.divider()
                up = st.file_uploader("Make a workbook the built-in history", type=["xlsx"], key="master")
                if up and st.button("Rebuild built-in history"):
                    info = build_slim_reference(_persist(up))
                    _players.clear()
                    st.success(f"Rebuilt: {len(info['copied'])} sheets, {info['size_kb']} KB.")
            st.session_state.paths = paths = staged
            st.session_state.names = _names = names
            _hero(staged)
            for w in _other_player_warnings(names):
                st.warning(w)

        with right, st.container(border=True):
            section("This run will use")
            html = (status_row("History", "ok", "your reference", names["reference"])
                    if staged.get("reference") else
                    status_row("History", "ok", f"built-in to {month_label(latest)}"))
            for k, lab in (("mis", "MIS"), ("transcript", "Expert call")):
                html += status_row(lab, "ok" if staged.get(k) else "no",
                                   "supplied" if staged.get(k) else "none", names.get(k, ""))
            others = [KINDS[k] for k in ("prior_call", "traffic", "logistics", "social", "brand_level", "secondary") if staged.get(k)]
            html += status_row("Other data", "ok" if others else "no",
                               ", ".join(others) if others else "none")
            st.markdown(html, unsafe_allow_html=True)
            if not staged.get("mis") and not staged.get("transcript"):
                st.caption("With no MIS or call, the output is built from history and is low confidence.")

    else:
        all_names = [r["name"] for r in rows]
        with st.container(border=True):
            section("Players", "The eight in your list are selected. Add or remove any.")
            chosen = st.multiselect("Players", all_names,
                                    default=[n for n in default_batch() if n in all_names],
                                    label_visibility="collapsed", key="batch_players")
            section("Files", "Drop everything at once. Each file is matched to a player by its name; "
                             "correct any that are wrong. Players with no files run on history.")
            files = st.file_uploader("Files", accept_multiple_files=True, key="batch_files",
                                     type=["xlsx", "xlsm", "xls", "docx", "csv", "txt"],
                                     label_visibility="collapsed")
            by_player: dict[str, dict[str, str]] = {}
            shared: dict[str, str] = {}                   # files marked "All selected players"
            ref_path, unassigned, ref_paths = None, [], []
            for f in files or []:
                path = _persist(f)
                kind = _sniff_cached(f.name, len(f.getbuffer()), path)
                who = match_player(f.name)
                c1, c2, c3 = st.columns([3.6, 2.6, 2.6])
                c1.markdown(f"**{f.name}**")
                k = c2.selectbox("type", list(KINDS), key=f"bk_{f.name}",
                                 index=list(KINDS).index(kind if kind in KINDS else "other"),
                                 format_func=KINDS.get, label_visibility="collapsed")
                if k == "reference":
                    c3.caption("used for the players it holds")
                    ref_path = path
                    ref_paths.append(path)
                    continue
                opts = ["— none —", ALL_PLAYERS] + all_names
                # Sector-wide data (traffic, logistics, social, ...) is not about one player, so a
                # file with no player in its name defaults to every selected player.
                default_i = (opts.index(who) if who in opts
                             else 1 if k in SHAREABLE else 0)
                pl = c3.selectbox("player", opts, key=f"bp_{f.name}",
                                  index=default_i, label_visibility="collapsed")
                if pl == "— none —":
                    unassigned.append(f.name)
                elif pl == ALL_PLAYERS:
                    shared[k] = path
                    if k in PLAYER_SPECIFIC:
                        st.caption(f"A {KINDS[k]} is about one player; applying it to all will stop "
                                   "or mislead the others.")
                else:
                    by_player.setdefault(pl, {})[k] = path
            # A player's own file of a kind wins over a shared one of the same kind.
            for n in chosen:
                if shared:
                    by_player[n] = {**shared, **by_player.get(n, {})}
            if shared and chosen:
                st.caption("Applied to all " + str(len(chosen)) + " selected players: "
                           + ", ".join(KINDS[k] for k in shared) + ".")
            st.session_state.batch_inputs = {"players": chosen, "files": by_player, "reference": ref_paths or None}
            for n in unassigned:
                st.warning(f"'{n}' is not assigned to a player and will not be used.")
            for pl in by_player:
                if pl not in chosen:
                    st.warning(f"Files were assigned to {pl}, who is not selected.")
        with st.container(border=True):
            section("What each player will use")
            tbl = []
            for n in chosen:
                f_ = by_player.get(n, {})
                tbl.append({"player": n, "MIS": "yes" if f_.get("mis") else "—",
                            "call": "yes" if f_.get("transcript") else "—",
                            "other": ", ".join(KINDS[k] for k in f_ if k not in ("mis", "transcript")) or "—",
                            "runs on": "MIS and call" if f_.get("mis") and f_.get("transcript") else
                                       "MIS" if f_.get("mis") else "call" if f_.get("transcript") else "history only"})
            if tbl:
                _stretch_df(pd.DataFrame(tbl), 420)
            st.caption("History: " + ("your uploaded reference" if ref_path else
                                      f"built-in, to {month_label(latest)}") + ".")
        _hero({"reference": ref_path} if ref_path else {})


# ================================================================= 2. WEIGHTS

with tab_weights:
    with st.container(border=True):
        section("Evidence weights", "How much each source counts. They must total 100%.")
        base = _weights_now()
        edited = st.data_editor(
            pd.DataFrame([{"source": k, "label": v["label"], "weight %": round(v["weight"] * 100, 2)}
                          for k, v in base["weights"].items()]),
            hide_index=True, width="stretch", disabled=["source", "label"],
            column_config={"source": st.column_config.TextColumn("Key", width="small"),
                           "label": st.column_config.TextColumn("Evidence source", width="large"),
                           "weight %": st.column_config.NumberColumn("Weight %", min_value=0.0,
                                                                     max_value=100.0, step=0.5,
                                                                     format="%.2f")},
            key="weights_editor")
        total = float(edited["weight %"].sum())
        (st.success if abs(total - 100) < 1e-6 else st.error)(
            f"Weights total {total:.2f}%" + ("" if abs(total - 100) < 1e-6 else ". Must be 100%."))
        wcfg = json.loads(json.dumps(base))
        for _, r in edited.iterrows():
            wcfg["weights"][r["source"]]["weight"] = float(r["weight %"]) / 100.0
        st.session_state.weights_cfg = wcfg
        if player:
            c1, c2 = st.columns([3, 1])
            wname = c1.text_input("Save as", placeholder=f"e.g. {player}-standard",
                                  label_visibility="collapsed")
            if c2.button("Save weight set", width="stretch") and wname:
                STORE.save_weights(wname, wcfg, player)
                st.success(f"Saved for {_p['name']}.")


# ================================================================= 3. RUN

def _extra_sources() -> dict[str, str]:
    return {k: v for k, v in paths.items()
            if k in ("traffic", "logistics", "social", "brand_level", "secondary", "other")}


def _run_single(growth_override: dict | None = None, month_=None, player_=None):
    return run_month(player=player_ or player, month=month_ or month,
                     reference_path=paths.get("reference"), mis_path=paths.get("mis"),
                     transcript_path=paths.get("transcript"),
                     overrides=list(st.session_state.get("edits", {}).values()),
                     prior_call_path=paths.get("prior_call"),
                     prior_call_month=st.session_state.get("prior_call_month"),
                     weights_cfg=st.session_state.get("weights_cfg"),
                     extra_sources=_extra_sources(), seasonality_mode=seasonality, store=STORE,
                     history_weight=hw / 100, growth_override=growth_override)


with tab_run:
    if not BATCH:
        with st.container(border=True):
            c1, c2 = st.columns([1, 3])
            go = c1.button("Run", type="primary", width="stretch")
            c2.caption(f"{_p['name']} · {month_label(month)}. Errors stop the run; warnings carry "
                       "into the audit workbook. With no MIS or call the run builds what it can "
                       "from history.")
        if go:
            st.session_state.growth_override = None
            st.session_state.edits = {}
            with st.spinner("Reading inputs, validating, estimating…"):
                st.session_state.result = _run_single()
        res = st.session_state.get("result")
        if res:
            conf = res.weights.confidence.value.title() if res.weights else "—"
            line = (f"{res.player.title()} · {month_label(res.month)} · {len(res.errors)} errors · "
                    f"{len(res.warnings)} warnings · {conf} confidence")
            if res.estimate is None:
                st.error("Run blocked by validation errors.")
            elif res.errors:
                st.warning(f"Completed with errors: {line}. Review before using.")
            else:
                st.success(f"Completed: {line}")
            all_f = sorted(res.all_findings, key=lambda x: ["error", "warning", "info"].index(x.severity.value))
            n_shown = sum(1 for f in all_f if f.severity is not Severity.INFO)
            with st.expander(f"Findings ({n_shown})", expanded=bool(res.errors) or res.estimate is None):
                show_info = st.checkbox("Include informational notes", value=False)
                fs = [f for f in all_f if show_info or f.severity is not Severity.INFO]
                if not fs:
                    st.caption("Nothing to review.")
                else:
                    _stretch_df(pd.DataFrame([{"severity": SEV_ICON[f.severity.value], "finding": f.message,
                                               "detail": f.detail} for f in fs]), 420,
                                severity=st.column_config.TextColumn("", width="small"),
                                finding=st.column_config.TextColumn("Finding", width="large"),
                                detail=st.column_config.TextColumn("Detail", width="large"))
    else:
        bi = st.session_state.get("batch_inputs", {"players": [], "files": {}, "reference": None})
        with st.container(border=True):
            c1, c2 = st.columns([1.2, 3])
            go = c1.button(f"Run {len(bi['players'])} players", type="primary", width="stretch",
                           disabled=not bi["players"])
            save_all = c2.checkbox("Save each month to history after running", value=False)
            c2.caption(f"{month_label(month)}. Each player runs on its own files, or on history if it "
                       "has none. One player failing never stops the others.")
        if go:
            bar = st.progress(0.0, text="Starting…")
            res_b = run_batch(
                bi["players"], month, files_by_player=bi["files"], reference_path=bi["reference"],
                weights_cfg=st.session_state.get("weights_cfg"), store=STORE,
                seasonality_mode=seasonality, history_weight=hw / 100, save_history=save_all,
                progress=lambda i, n, name: bar.progress(i / max(n, 1),
                                                         text=f"Running {name}…" if name else "Done"))
            bar.empty()
            st.session_state.batch_result = res_b
            zp = res_b.zip_to(Path("outputs") / zip_name(month)) if res_b.done else None
            st.session_state.batch_zip = str(zp) if zp else None
        rb = st.session_state.get("batch_result")
        if rb:
            st.success(f"{rb.done} of {len(rb.items)} players completed. See the Results tab.")
            for i in [i for i in rb.items if i.status != "done"]:
                st.warning(f"{i.player}: {i.status}. {i.message}")


# ================================================================= 4. REVIEW / RESULTS

def _growth_tiles(est, ref, P, res):
    prior, yoy_m = shift_months(res.month, -1), shift_months(res.month, -12)
    items = []
    for lab, key, unit in (("Gross GMV", "gross_gmv", "INR Cr"), ("Shipped GMV", "shipped_gmv", "INR Cr"),
                           ("Net GMV", "net_gmv", "INR Cr"), ("AOV", "aov", "INR"), ("ASP", "asp", "INR")):
        v = est.get(P.get(key, -1))
        if v is None or v.is_null:
            items.append(tile(lab, "n/a", "unavailable"))
            continue
        row = P[key]
        ya, pm = ref.value(row, yoy_m), ref.value(row, prior)
        bits = []
        if ya:
            bits.append(f"YoY {v.value / ya - 1:+.1%}")
        if pm:
            bits.append(f"MoM {v.value / pm - 1:+.1%}")
        rc = est.reconciliations.get(241) if key == "gross_gmv" else None
        if rc is not None and rc.final_level and abs(rc.final_level / rc.level - 1) > 1e-9:
            bits.append(f"MIS {rc.level:,.1f}")
        items.append(tile(f"{lab} ({unit})", _fmt(v.value), v.provenance.value, " · ".join(bits)))
    tiles(items)


def render_review(res):
    cfg = res.config or load_player_config(res.player)
    P, est, ref, cal, wres = cfg["platform_rows"], res.estimate, res.reference, res.calibration, res.weights

    _growth_tiles(est, ref, P, res)

    rc = est.reconciliations.get(241)
    ov = st.session_state.get("growth_override") or {}
    _m = ov.get("mom", rc.mom_stated if rc and rc.mom_stated is not None else (rc.mom_final if rc else None))
    _y = ov.get("yoy", rc.yoy_stated if rc and rc.yoy_stated is not None else (rc.yoy_final if rc else None))
    with st.expander(f"Growth settings: MoM {_pct(_m) or 'not set'} · YoY {_pct(_y) or 'not set'}"
                     + (" (edited)" if ov else ""), expanded=bool(ov)):
        st.caption("Measured on your stored history. Change either rate and the output recalculates around it.")
        if rc is None and not ov:
            st.caption("The MIS stated no growth for this player. Enter MoM and YoY below to "
                       "balance the output against them.")
        shown_mom = ov.get("mom", rc.mom_stated if rc and rc.mom_stated is not None else
                           (rc.mom_final if rc and rc.mom_final is not None else 0.0))
        shown_yoy = ov.get("yoy", rc.yoy_stated if rc and rc.yoy_stated is not None else
                           (rc.yoy_final if rc and rc.yoy_final is not None else 0.0))
        c1, c2, c3, c4 = st.columns([1, 1, 1, 1])
        mom_in = c1.number_input("MoM %", value=round(shown_mom * 100, 2), step=0.5, format="%.2f",
                                 key=f"mom_in_{res.run_id}")
        yoy_in = c2.number_input("YoY %", value=round(shown_yoy * 100, 2), step=0.5, format="%.2f",
                                 key=f"yoy_in_{res.run_id}")
        c3.markdown("&nbsp;", unsafe_allow_html=True)
        if c3.button("Recalculate", type="primary", width="stretch"):
            st.session_state.growth_override = {"mom": mom_in / 100, "yoy": yoy_in / 100}
            with st.spinner("Recalculating…"):
                st.session_state.result = _run_single(st.session_state.growth_override,
                                                      res.month, res.player)
            st.rerun()
        c4.markdown("&nbsp;", unsafe_allow_html=True)
        if c4.button("Reset to the MIS", width="stretch", disabled=not ov):
            st.session_state.growth_override = None
            st.session_state.result = _run_single(None, res.month, res.player)
            st.rerun()
        if rc is not None and rc.final_level:
            tiles([tile("YoY on stored history", _pct(rc.yoy_final if rc.yoy_final is not None
                                                      else rc.yoy_stored),
                        note=f"stated {_pct(rc.yoy_stated)}"),
                   tile("MoM on stored history", _pct(rc.mom_final if rc.mom_final is not None
                                                      else rc.mom_stored),
                        note=f"stated {_pct(rc.mom_stated)}")], tight=True)
        elif ov:
            st.info("These rates did not change the figures: this player's GMV is anchored on "
                    "shipped GMV, not gross GMV.")

    section("Category GMV", "Shipped GMV, INR Cr")
    items = []
    for lab, par in [(lf["label"], lf["gmv"]) for lf in cfg["category_tree"]["leaves"]]:
        v = est.value_for_par(ref, par)
        ok = v is not None and not v.is_null
        items.append(tile(lab, _fmt(v.value) if ok else "n/a", v.provenance.value if ok else "unavailable"))
    tiles(items, tight=True)

    b1, b5, b2, b3, b4 = st.columns(5)
    for col, (key, lab) in zip((b1, b5, b2, b3), [("deliverable", "Output workbook"),
                                                  ("analysis", "Analysis workbook"),
                                                  ("audit", "Audit workbook"),
                                                  ("model_column", "Model column")]):
        p = Path(res.outputs[key])
        if p.exists():
            col.download_button(lab, p.read_bytes(), file_name=p.name, width="stretch",
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if b4.button("Save to history", width="stretch", disabled=bool(res.errors),
                 help=f"Stores this month so {month_label(shift_months(res.month, 1))} builds on it."):
        info = commit_history(res, STORE)
        _players.clear()
        st.success(f"Saved {month_label(res.month)}: {info['values']} values.")

    ch_file = Path(res.outputs.get("channel", ""))
    if ch_file.is_file():
        c1, c2 = st.columns([1, 4])
        c1.download_button("Channel Distribution", ch_file.read_bytes(), file_name=ch_file.name,
                           width="stretch",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        c2.caption("Your Channel Distribution sheet with this month added. Online share and the "
                   "channel category mixes are estimates, reconciled to this run's category shares.")

    with st.expander("Trend and deviation"):
        used = "your uploaded reference" if paths.get("reference") else "the built-in history"
        st.caption(f"Against {used} ({reference_months(ref)}). Percent metrics in points.")
        tr = trend_rows(est, ref, cfg, res.month)
        if tr:
            n_ = lambda x: None if x is None else x * 100
            tdf = pd.DataFrame([{
                "metric": t["metric"], month_label(res.month): _fmt(t["value"], t["unit"]),
                "last month": _fmt(t["prior_month"], t["unit"]) if t["prior_month"] is not None else "",
                "vs last month": n_(t["mom"]),
                "last year": _fmt(t["year_ago"], t["unit"]) if t["year_ago"] is not None else "",
                "vs last year": n_(t["yoy"]),
                "reference had": _fmt(t["reference_figure"], t["unit"]) if t["reference_figure"] is not None else "",
                "vs reference": n_(t["vs_reference"]), "flag": t["flag"]} for t in tr])
            _stretch_df(_tinted(tdf, ["vs last month", "vs last year", "vs reference"], 30.0), 460,
                        flag=st.column_config.TextColumn(width="large"))
            st.caption("Growth is in % (points for percentage metrics). Red is a fall, "
                       "yellow is flat and green is a rise.")

    with st.expander("Evidence weights and adjustments"):
        _stretch_df(pd.DataFrame(wres.table())[["source", "configured_pct", "effective_pct"]], 330,
                    source=st.column_config.TextColumn("Source", width="medium"),
                    configured_pct=st.column_config.NumberColumn("Set", format="%.0f%%"),
                    effective_pct=st.column_config.NumberColumn("Used", format="%.0f%%"))
        for w in wres.warnings:
            st.caption("⚠️ " + w)
        if est.adjustments:
            _stretch_df(pd.DataFrame([x.to_dict() for x in est.adjustments])[["metric", "driver", "rationale"]],
                        330, rationale=st.column_config.TextColumn(width="large"))

    _metric_editor(res, est, ref)


def _metric_editor(res, est, ref) -> None:
    """Every metric in the output sheet, in the output's own order. Values can be edited in place."""
    from furniture_intel.export.workbook import ordered_template
    cfg = res.config or load_player_config(res.player)
    month = res.month
    m3 = [shift_months(month, -2), shift_months(month, -1), month]          # e.g. Jul, Aug, Sep
    yago = lambda m: shift_months(m, -12)
    g = lambda a, b: (a / b - 1) * 100 if a is not None and b else None

    base = []                                                               # what the run produced
    seen: set[int] = set()
    for t, sec_name in ordered_template(ref, est, cfg):
        if t.par_id in seen:               # the furniture template lists a par_id twice; edit it once
            continue
        seen.add(t.par_id)
        v = est.value_for_par(ref, t.par_id)
        has = v is not None and not v.is_null and v.value is not None
        row = ref.par_to_row.get(t.par_id)
        sc = 100.0 if t.unit == "%" else 1.0
        base.append({"par": t.par_id, "section": sec_name or "", "question": (t.parent_question or "").strip(),
                     "metric": t.parameter_name.strip(), "unit": t.unit or "", "scale": sc,
                     "orig": round(v.value * sc, 4) if has else None,
                     "source": PROV_LABEL.get(v.provenance.value, "") if has else "", "row": row})

    draft = st.session_state.setdefault("draft", {})
    edits = st.session_state.setdefault("edits", {})

    def growth_cols(r, shown):
        """MoM and YoY for the last three months; the current month follows the value shown."""
        h = (lambda m: ref.value(r["row"], m)) if r["row"] else (lambda m: None)
        cur = shown / r["scale"] if shown is not None else None
        out = {}
        for m in m3:
            now = cur if m == month else h(m)
            out[f"MoM {month_label(m)}"] = g(now, h(shift_months(m, -1)))
        for m in m3:
            now = cur if m == month else h(m)
            out[f"YoY {month_label(m)}"] = g(now, h(yago(m)))
        return out

    recs = []
    for r in base:
        shown = draft.get(r["par"], r["orig"])
        recs.append({"par": r["par"], "section": r["section"], "question": r["question"],
                     "metric": r["metric"], "unit": r["unit"], "value": shown, "source":
                     "Edited" if r["par"] in draft else r["source"], **growth_cols(r, shown)})

    with st.expander("Key metrics: edit values"):
        st.caption("Every figure in the output, in its order. Change a value and the MoM and YoY beside "
                   "it update straight away; click Apply to put it into the output files. "
                   "The last three months of growth are shown to help you judge a change.")
        if not recs:
            st.caption("No metrics to show.")
            return
        df = pd.DataFrame(recs)
        f1, f2 = st.columns([3, 2])
        groups = list(dict.fromkeys(df["section"]))
        pick = f1.multiselect("Show", groups, default=[x for x in groups if x != "Not available this month"],
                              key=f"edit_groups_{res.run_id}")
        q = f2.text_input("Search", key=f"edit_q_{res.run_id}", placeholder="e.g. AOV, Tier, COD, price")
        view = df[df["section"].isin(pick)]
        if q:
            hit = view["metric"].str.contains(q, case=False, regex=False) | \
                view["question"].str.contains(q, case=False, regex=False)
            view = view[hit]
        show = view.set_index("par")
        gcols = [c for c in show.columns if c.startswith(("MoM ", "YoY "))]
        sig = f"{len(draft)}|{','.join(map(str, pick))}|{q}"                  # a new view starts clean
        edited = st.data_editor(
            show.style.map(_tint(30.0), subset=gcols), width="stretch", height=_h(len(show), 560), hide_index=True,
            key=f"metric_editor_{res.run_id}_{abs(hash(sig))}",
            disabled=[c for c in show.columns if c != "value"],
            column_config={"section": None, "question": None,
                           "metric": st.column_config.TextColumn("Metric", width="large"),
                           "unit": st.column_config.TextColumn("Unit", width="small"),
                           "value": st.column_config.NumberColumn("Value", format="%.4f"),
                           "source": st.column_config.TextColumn("Source", width="small"),
                           **{c: st.column_config.NumberColumn(c, format="%+.1f%%") for c in gcols}})

        origs = {r["par"]: r["orig"] for r in base}
        moved = False
        for p in show.index:
            new, was = edited.loc[p, "value"], show.loc[p, "value"]
            if pd.isna(new) or (pd.notna(was) and abs(float(new) - float(was)) <= 1e-9):
                continue
            o = origs.get(int(p))
            if o is not None and abs(float(new) - o) <= 1e-9:
                draft.pop(int(p), None)
            else:
                draft[int(p)] = float(new)
            moved = True
        if moved:
            st.rerun()                                      # redraw MoM and YoY from the new value

        b1, b2, b3 = st.columns([1.3, 1.3, 3])
        if b1.button(f"Apply {len(draft)} change(s)" if draft else "Apply changes",
                     type="primary", width="stretch", disabled=not draft):
            unit_of = {r["par"]: r["unit"] for r in base}
            name_of = {r["par"]: r["metric"] for r in base}
            row_of = {r["par"]: r["row"] for r in base}
            for p, newv in draft.items():
                edits[p] = Override(
                    metric=name_of[p], row=row_of[p],
                    original=edits[p].original if p in edits else (
                        origs[p] / (100.0 if unit_of[p] == "%" else 1.0) if origs.get(p) is not None else None),
                    new_value=newv / 100.0 if unit_of[p] == "%" else newv,
                    reason="Edited in the metrics table", author="portal", par=p, unit=unit_of[p])
            st.session_state.draft = {}
            STORE.save_overrides(res.run_id, list(edits.values()))
            with st.spinner("Applying…"):
                st.session_state.result = _run_single(st.session_state.get("growth_override"),
                                                      res.month, res.player)
            st.rerun()
        if b2.button("Clear my edits", width="stretch", disabled=not (edits or draft)):
            st.session_state.edits, st.session_state.draft = {}, {}
            st.session_state.result = _run_single(st.session_state.get("growth_override"),
                                                  res.month, res.player)
            st.rerun()
        if edits or draft:
            b3.caption(f"{len(edits)} applied, {len(draft)} waiting to apply. The original and the change "
                       "are kept in the audit workbook; figures built from an edited one are not recomputed.")


def render_results(rb):
    section(f"{rb.done} of {len(rb.items)} players, {month_label(rb.month)}")
    df = pd.DataFrame([{
        "player": r["player"], "status": r["status"], "gross GMV": r.get("gross GMV"),
        "shipped GMV": r.get("shipped GMV"), "MoM": r.get("MoM"), "YoY": r.get("YoY"), "source": PROV_LABEL.get(r.get("source", ""), ""),
        "errors": r.get("errors"), "warnings": r.get("warnings"), "confidence": r.get("confidence", ""),
        "ran on": r["files used"], "note": r["message"]} for r in rb.rows()])
    st.dataframe(_tinted(df, ["MoM", "YoY"], 30.0), hide_index=True, width="stretch", height=_h(len(df), 460),
                 column_config={"gross GMV": st.column_config.NumberColumn(format="%.1f"),
                                "shipped GMV": st.column_config.NumberColumn(format="%.1f"),
                                "MoM": st.column_config.NumberColumn("MoM %", format="%+.1f"),
                                "YoY": st.column_config.NumberColumn("YoY %", format="%+.1f")})
    z = st.session_state.get("batch_zip")
    if z and Path(z).exists():
        st.download_button("Download everything (zip)", Path(z).read_bytes(), file_name=Path(z).name,
                           type="primary", mime="application/zip")
    cp = getattr(rb, "collated", None)
    if cp and Path(cp).exists():
        st.download_button("Comparison of all players (Excel)", Path(cp).read_bytes(), file_name=Path(cp).name,
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with st.expander("Individual files"):
        for it in rb.items:
            if it.result is None or not it.result.outputs:
                continue
            st.markdown(f"**{it.player}**")
            cols = st.columns(5)
            for col, (key, lab) in zip(cols, [("deliverable", "Output"), ("analysis", "Analysis"),
                                              ("audit", "Audit"), ("model_column", "Model column"),
                                              ("channel", "Channel Distribution")]):
                p = Path(it.result.outputs.get(key, ""))
                if p.exists():
                    col.download_button(lab, p.read_bytes(), file_name=p.name, key=f"dl_{it.player}_{key}",
                                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


with tab_review:
    if not BATCH:
        res = st.session_state.get("result")
        if not res or res.estimate is None:
            st.info("Run the month first (step 3).")
        else:
            render_review(res)
    else:
        rb = st.session_state.get("batch_result")
        if not rb:
            st.info("Run the players first (step 3).")
        else:
            render_results(rb)


# ================================================================= 5. HISTORY

with tab_history:
    who = player
    if BATCH:
        who = by_name[st.selectbox("Player", [r["name"] for r in rows])]["key"]
    a, b = st.columns([3, 2], gap="large")
    with a:
        runs = STORE.list_runs(who)
        with st.expander(f"Previous runs ({len(runs)})", expanded=False):
            if not runs:
                st.caption("No runs yet for this player.")
            else:
                _stretch_df(pd.DataFrame([{
                    "run": r.get("run_id"), "month": month_label(r["month"]) if r.get("month") else "",
                    "status": r.get("status"), "errors": r.get("errors"), "warnings": r.get("warnings")}
                    for r in runs]), 300)
                pick = st.selectbox("Open a run", ["—"] + [r["run_id"] for r in runs])
                if pick != "—":
                    mf = STORE.load_manifest(pick)
                    for key, pth in (mf.get("outputs") or {}).items():
                        pp = Path(pth)
                        if pp.exists():
                            st.download_button(f"Download {key}", pp.read_bytes(), file_name=pp.name,
                                               key=f"dl_{pick}_{key}")
                    st.json(mf, expanded=False)
    with b, st.container(border=True):
        section("Months saved to history", "These feed the next month's run.")
        sm = history_describe(STORE.root, who)
        if not sm:
            st.caption("Nothing saved yet.")
        for m in sm:
            c1, c2 = st.columns([4, 1])
            c1.markdown(f"**{month_label(m['month'])}** · {m['values']} values  \n"
                        f"<span style='color:{MUTED};font-size:.8rem'>{m['basis'].replace('_', ' ')}</span>",
                        unsafe_allow_html=True)
            if c2.button("Remove", key=f"rm_{who}_{m['month']}"):
                remove_month(STORE.root, who, m["month"])
                _players.clear()
                st.rerun()

    with st.container(border=True):
        from furniture_intel.learning import trend as _learn_trend
        section("Is the gap closing?",
                "How far the model's own view was from the MIS and from the expert each saved month. "
                "Lower is better; a falling line means the model is learning.")
        lt = _learn_trend(calibration_records(STORE.root, who, "9999-12"))
        if len(lt) < 1:
            st.caption("Save a month to history after a run and this fills in.")
        else:
            df = pd.DataFrame(lt)
            df["month"] = df["month"].map(month_label)
            show = df.set_index("month").astype(float)
            if len(show) >= 2:
                st.line_chart(show[[c for c in ("platform vs MIS", "categories vs MIS",
                                                "categories vs expert", "platform vs expert",
                                                "edited vs model")
                                    if show[c].notna().any()]], height=240)
            gap_df = (show * 100).round(1).rename(columns=lambda c: c + " (%)")
            st.dataframe(gap_df.style.map(lambda x: _tint(30.0)(-x if pd.notna(x) else x)),
                         width="stretch")


# ================================================================= 6. COLLATE

with tab_collate:
    with st.container(border=True):
        section("Collate players' analysis files",
                "Add the Analysis workbooks of any players, from any run. You get one Excel file with a "
                "comparison of all of them and a tab for each player, with its full history.")
        ups = st.file_uploader("Analysis workbooks", accept_multiple_files=True, type=["xlsx"],
                               key="collate_files", label_visibility="collapsed")
        pool: list[str] = [_persist(u) for u in ups or []]
        last = st.session_state.get("batch_result")
        if last is not None:
            use_last = st.checkbox("Also include the players from the last several-players run", value=True)
            if use_last:
                pool += [it.result.outputs["analysis"] for it in last.items
                         if it.result is not None and it.result.outputs.get("analysis")
                         and Path(it.result.outputs["analysis"]).exists()]
        one = st.session_state.get("result")
        if one is not None and one.outputs.get("analysis") and Path(one.outputs["analysis"]).exists() \
                and st.checkbox(f"Also include this session's {one.player} run", value=False):
            pool.append(one.outputs["analysis"])
        if not pool:
            st.caption("Nothing added yet.")
        elif st.button(f"Build the collated workbook from {len(pool)} file(s)", type="primary"):
            from furniture_intel.export.collate import collate_analysis_files
            collate_target = Path("outputs") / f"All players analysis {month_label(month).replace(chr(39), ' 20')}.xlsx"
            try:
                try:
                    out, problems = collate_analysis_files(pool, collate_target)
                except PermissionError:
                    # the earlier collated file is open in Excel: write beside it instead of failing
                    import datetime as _dt
                    alt = collate_target.with_name(f"{collate_target.stem} {_dt.datetime.now():%H%M%S}.xlsx")
                    out, problems = collate_analysis_files(pool, alt)
                    problems.append(f"{collate_target.name} is open in Excel, so this was saved as {alt.name}.")
                st.session_state.collated_out = (str(out), problems)
            except Exception as e:
                st.session_state.collated_out = None
                st.error(f"Could not collate: {type(e).__name__}: {e}")
        co = st.session_state.get("collated_out")
        if co and Path(co[0]).exists():
            st.success("Built. One tab per player, plus the comparison tabs.")
            for pr in co[1]:
                st.warning(pr)
            st.download_button("Download the collated workbook", Path(co[0]).read_bytes(),
                               file_name=Path(co[0]).name, type="primary",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ================================================================= 7. EDITED ANALYSIS -> OUTPUT

with tab_rebuild:
    with st.container(border=True):
        section("Make Output files from edited Analysis workbooks",
                "Download each player's Analysis workbook, change the figures in its current-month column, and "
                "upload them all here, one workbook per player. Each Output file is rewritten from its workbook, "
                "replacing the one already saved. Nothing is re-estimated: what is in the column is what goes out.")
        ups = st.file_uploader("Analysis workbooks", type=["xlsx"], accept_multiple_files=True,
                               key="rebuild_files", label_visibility="collapsed")
        learn = st.checkbox("Learn from my edits", value=True, key="rebuild_learn",
                            help="Keep the edited month as history, so later months build on your figures, and "
                                 "correct the category mix by how far your edits moved the shares.")
        if ups and st.button(f"Rebuild {len(ups)} Output file{'s' if len(ups) > 1 else ''}", type="primary"):
            from furniture_intel.export.from_analysis import rebuild_output
            results = []
            for u in ups:
                try:
                    out, info = rebuild_output(_persist(u), "outputs", STORE.root if learn else None)
                    results.append({"file": u.name, "path": str(out), "info": info, "error": None})
                except PermissionError:
                    results.append({"file": u.name, "error": "The existing Output file is open in Excel. "
                                                              "Close it and try again."})
                except Exception as e:
                    results.append({"file": u.name, "error": f"Could not rebuild: {e}"})
            st.session_state.rebuilt = results
            if learn:
                _players.clear()                         # saved history changed
        done = []
        for i, r_ in enumerate(st.session_state.get("rebuilt") or []):
            if r_.get("error"):
                st.error(f"{r_['file']}: {r_['error']}")
                continue
            o, info = Path(r_["path"]), r_["info"]
            if not o.exists():
                continue
            done.append(o)
            st.success(f"{info['player']} {month_label(info['month'])}: Output file rewritten at {o}")
            st.caption(f"{info['values']} figures. {info['edited']} changed from the run, {info['filled']} "
                       f"added, {info['cleared']} cleared. Changed and added figures are marked Assumed on the "
                       "Source sheet." + ("" if info["had_run_data"] else
                                          " This file predates edit tracking, so edits could not be told apart."))
            lr = info.get("learned")
            if lr:
                gap = f" Headline figures moved {lr['headline_gap']:.1%} on average." if lr.get("headline_gap") is not None else ""
                st.caption(f"Learned: {month_label(info['month'])} saved to history with {lr['saved_values']} of "
                           f"your figures; {lr['categories_learned']} category shares will correct next "
                           f"month's mix.{gap}")
            st.download_button("Download the Output file", o.read_bytes(), file_name=o.name, key=f"rb_dl_{i}",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if len(done) > 1:
            import io, zipfile
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for o in done:
                    z.write(o, o.name)
            st.download_button(f"Download all {len(done)} Output files (zip)", buf.getvalue(),
                               file_name="Output files.zip", type="primary", key="rb_dl_zip",
                               mime="application/zip")

    with st.container(border=True):
        section("Add the historical numbers to Analysis workbooks you already edited",
                "Some categories (ASP, GMV) have blank past months in the Analysis. Upload the edited files and get "
                "each back with those past-month and year-ago cells filled from the stored history. Only blank cells "
                "are written: your edits and formulas are not touched, and the MoM, YoY and average columns update.")
        old_ups = st.file_uploader("Edited Analysis workbooks", type=["xlsx"], accept_multiple_files=True,
                                   key="hist_files", label_visibility="collapsed")
        if old_ups and st.button(f"Add historical numbers to {len(old_ups)} file{'s' if len(old_ups) > 1 else ''}",
                                 key="hist_go"):
            from furniture_intel.export.from_analysis import fill_history_in_analysis
            fixed = []
            for u in old_ups:
                src = _persist(u)
                dst = Path(src).with_name(f"{Path(u.name).stem} (history added).xlsx")
                try:
                    info = fill_history_in_analysis(src, dst)
                    fixed.append({"file": u.name, "path": str(dst), "filled": info["filled"], "rows": info["rows"],
                                  "error": None})
                except Exception as e:
                    fixed.append({"file": u.name, "error": str(e)})
            st.session_state.hist_fixed = fixed
        hist_done = [f_ for f_ in (st.session_state.get("hist_fixed") or [])
                     if not f_.get("error") and Path(f_["path"]).exists()]
        if len(hist_done) > 1:
            import io, zipfile
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for f_ in hist_done:
                    z.write(f_["path"], Path(f_["path"]).name)
            st.download_button(f"Download all {len(hist_done)} files (zip)", buf.getvalue(),
                               file_name="Analysis workbooks (history added).zip", mime="application/zip",
                               type="primary", key="hist_dl_all")
        for i, f_ in enumerate(st.session_state.get("hist_fixed") or []):
            if f_.get("error"):
                st.error(f"{f_['file']}: {f_['error']}")
            elif Path(f_["path"]).exists():
                st.success(f"{f_['file']}: {f_['filled']} cells filled across {f_['rows']} rows.")
                st.download_button("Download", Path(f_["path"]).read_bytes(), file_name=Path(f_["path"]).name,
                                   key=f"hist_dl_{i}",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.caption("Saved as a new file; Excel comments added as threaded replies are not carried across.")


# ================================================================= 8. WEBFORM

with tab_webform:
    with st.container(border=True):
        section("Fill the webforms from Output files",
                "Upload one or more Output workbooks. Each comes back in its player's webform layout with every par_id "
                "filled from the Output, in the webform layout; a par_id the Output has no figure for "
                "is null.")
        wf_outs = st.file_uploader("Output workbooks", type=["xlsx"], accept_multiple_files=True, key="wf_outputs")
        if wf_outs and st.button(f"Fill {len(wf_outs)} webform{'s' if len(wf_outs) > 1 else ''}", type="primary"):
            import re
            from furniture_intel.export.webform import fill_webform, output_label, saved_layout
            res = []
            for u in wf_outs:
                try:
                    src = _persist(u)
                    player, month = output_label(src)
                    layout = saved_layout()
                    stem = "_".join(x for x in (player.replace(" ", "_"), month.replace("'", "_20").replace(" ", "_")) if x)
                    r = fill_webform(src, layout, Path("outputs") / "webforms" / f"{stem}.csv")
                    res.append({"file": u.name, "name": f"{stem}.csv", **r})
                except Exception as e:
                    res.append({"file": u.name, "error": f"Could not fill: {e}"})
            st.session_state.webforms = res
        done = [r for r in (st.session_state.get("webforms") or []) if not r.get("error")]
        if len(done) > 1:
            import io, zipfile
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for r in done:
                    z.write(r["path"], r["name"])
            st.download_button(f"Download all {len(done)} webforms (zip)", buf.getvalue(),
                               file_name="Furniture_webforms.zip", mime="application/zip", type="primary", key="wf_dl_all")
        for i, r in enumerate(st.session_state.get("webforms") or []):
            if r.get("error"):
                st.error(f"{r['file']}: {r['error']}")
                continue
            c1, c2 = st.columns([3, 1])
            c1.success(f"{r['name']}: {r['filled']} of {r['rows']} rows filled, {r['null']} null.")
            c2.download_button("Download", Path(r["path"]).read_bytes(), file_name=r["name"], mime="text/csv",
                               key=f"wf_dl_{i}")


def openpyxl_titles(path) -> list[str]:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True)
    names = list(wb.sheetnames)
    wb.close()
    return names


with tab_qc:
    import csv as _csv
    from furniture_intel.export import clientqc as cq
    from furniture_intel.export.webform import LAYOUT as _WF_LAYOUT, output_label, output_values
    with st.container(border=True):
        section("QC the client sheet against our Outputs",
                "Upload the client sheet once and the Output files for every player you want checked. Each Output is paired "
                "with its tab in the client sheet by player name. The client sheet has no par_ids, so its rows are matched to "
                "ours by their values across the shared history, then by name. You get one workbook: for each player, the "
                "client sheet's rows with the client's last months beside ours, the difference and a status.")
        cfile = st.file_uploader("Client sheet", type=["xlsx"], key="qc_client")
        qc_outs = st.file_uploader("Our Output or Analysis workbooks (all the players, one file each)", type=["xlsx"],
                                   accept_multiple_files=True, key="qc_outputs")
        if cfile and qc_outs:
            cpath = _persist(cfile)
            sheets = cq.client_sheets(cpath)
            raw_tabs = [t for t in sheets if not t.lower().endswith("_formatted")]
            if not raw_tabs:
                st.error("No tab in this client sheet looks like player data (a Parameter column with month headers). "
                         "Tabs found: " + ", ".join(openpyxl_titles(cpath)))
            else:
                st.caption("Client tabs found: " + ", ".join(raw_tabs))
            _bi = (st.session_state.get("batch_inputs") or {}).get("reference")
            qc_ref = _bi[0] if isinstance(_bi, list) and _bi else BUILTIN_PATH
            known = {r["name"] for r in rows}
            pairs = []
            st.markdown("**Pairing**")
            for i, u in enumerate(qc_outs):
                try:
                    opath = _persist(u)
                    fig_ = cq.read_figures(opath)
                    pl_, mo_ = fig_["player"], fig_["label"]
                    mk_ = cq.month_key(mo_.replace(" ", "'"))
                except Exception as e:
                    st.error(f"{u.name}: could not read it as an Output or Analysis workbook ({e})")
                    continue
                canon = next((n for n in known if cq.norm(n).replace(" ", "") == cq.norm(pl_).replace(" ", "")), None)
                pl_ = canon or pl_
                guess = cq.tab_for(pl_, raw_tabs)
                c1, c2, c3 = st.columns([3, 2, 3])
                c1.write(f"{u.name}")
                c2.write(f"{pl_}, {mo_}")
                tab = c3.selectbox("Client tab", ["(skip)"] + raw_tabs, key=f"qc_tab_{i}_{u.name}",
                                   index=(raw_tabs.index(guess) + 1) if guess else 0, label_visibility="collapsed")
                if tab != "(skip)" and canon and mk_:
                    pairs.append({"player": pl_, "month": mk_, "label": mo_, "tab": tab, "output": opath})
                elif tab != "(skip)":
                    st.warning(f"{u.name}: " + ("player not in this portal's list" if not canon else "month not readable") +
                               f" (read as '{pl_}', '{mo_}'), skipped.")
            if not pairs:
                st.warning("None of the uploaded Outputs is paired with a client tab yet, so there is nothing to QC. "
                           "The client sheet has tabs for: " + (", ".join(raw_tabs) or "none") +
                           ". Pick the tab for each Output above, or upload the Outputs of those players.")
            t1, t2 = st.columns(2)
            tol_val = t1.number_input("Tolerance on values (%)", 0.0, 100.0, 2.0, 0.5, key="qc_tv") / 100
            tol_pp = t2.number_input("Tolerance on percentages (points)", 0.0, 20.0, 0.5, 0.1, key="qc_tp")
            if st.button(f"Run QC for {len(pairs)} player{'s' if len(pairs) != 1 else ''}", type="primary", key="qc_run",
                         disabled=not pairs):
                extra = {int(r_["par_id"]): r_["parameter_name"]
                         for r_ in _csv.DictReader(open(_WF_LAYOUT, encoding="utf-8-sig")) if r_["par_id"].isdigit()}
                runs, bar = [], st.progress(0.0)
                for k, p_ in enumerate(pairs):
                    try:
                        cl = cq.read_client(cpath, p_["tab"])
                        if p_["month"] not in cl["months"]:
                            st.info(f"{p_['player']}: the client tab {p_['tab']} has no {p_['label']} figures yet, so its latest "
                                    "month is judged and our " + p_['label'] + " Output sits in the last column.")
                        runs.append(cq.run_player(p_["player"], cl, qc_ref,
                                                  cq.read_figures(p_["output"])["values"],
                                                  p_["month"], extra, tol_val, tol_pp))
                    except Exception as e:
                        st.error(f"{p_['player']}: {e}")
                    bar.progress((k + 1) / len(pairs))
                bar.empty()
                st.session_state.qc_runs = {"runs": runs, "tv": tol_val, "tp": tol_pp}
        S = st.session_state.get("qc_runs")
        if S and S["runs"]:
            runs = S["runs"]
            out_p = Path("outputs") / "client_qc" / "Client_QC.xlsx"
            out_p.parent.mkdir(parents=True, exist_ok=True)
            cq.write_qc(out_p, runs, S["tv"], S["tp"])
            st.download_button(f"Download the QC workbook ({len(runs)} player{'s' if len(runs) > 1 else ''})", out_p.read_bytes(),
                               file_name="Client_QC.xlsx", key="qc_dl", type="primary",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            for k, run in enumerate(runs):
                res = run["results"]
                cnt = Counter(x["status"] for x in res)
                with st.expander(f"{run['player']}, judged {run['month']}"
                                 + (f", Output {run['out_month']} shown" if run["out_month"] != run["month"] else "") + ": " + ", ".join(f"{v} {s_}" for s_, v in cnt.most_common(4)),
                                 expanded=len(runs) == 1):
                    only = st.checkbox("Only rows that need a look", value=True, key=f"qc_only_{k}")
                    rows_ = [x for x in res if x["status"] in cq.REVIEW] if only else res
                    mo = res[0]["months"] if res else []
                    df = pd.DataFrame([{"Section": x["parent"], "Parameter": x["name"], "Units": x["unit"], "par_id": x["par_id"],
                                        **{f"Client {m}": v for m, v in zip(mo, x["c_win"])},
                                        **{f"Ours {m}": v for m, v in zip(mo, x["o_win"])},
                                        "Difference": x["diff"], "Status": x["status"]} for x in rows_])
                    st.dataframe(df, hide_index=True, use_container_width=True, height=_h(len(df)))
                    with st.expander("Fix doubtful matches"):
                        has_data = {r_["row"] for r_ in run["client"]["rows"] if r_["series"]}
                        fix = [m for m in run["mapping"] if cq.confidence(m) in ("unmatched", "name only", "medium")
                               and m["row"] in has_data]
                        labels = {p: f"{p} | {run['ours'].get(p, {}).get('name') or run['extra'].get(p, '')}"
                                  for p in sorted(set(run["ours"]) | set(run["extra"]))}
                        inv = {v: k_ for k_, v in labels.items()}
                        ed = pd.DataFrame([{"Section": m["parent"], "Client parameter": m["name"], "Now": cq.confidence(m),
                                            "Our metric": labels.get(m["par_id"], "")} for m in fix])
                        new = st.data_editor(ed, hide_index=True, use_container_width=True, key=f"qc_fix_{k}",
                                             column_config={"Our metric": st.column_config.SelectboxColumn(
                                                 options=[""] + list(labels.values()))},
                                             disabled=["Section", "Client parameter", "Now"])
                        if st.button("Save these matches", key=f"qc_save_{k}"):
                            over = cq.load_overrides(run["player"])
                            for m, (_i, r_) in zip(fix, new.iterrows()):
                                over[cq.key_of(m)] = inv.get(r_["Our metric"]) if r_["Our metric"] else None
                            cq.save_overrides(run["player"], over)
                            st.success("Saved for this player. Run QC again to refresh.")
        elif not (cfile and qc_outs):
            st.caption("Upload the client sheet and at least one Output to begin.")
