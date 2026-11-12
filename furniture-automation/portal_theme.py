"""Portal styling: one stylesheet driven by colour variables, light or dark.

Streamlit cannot switch its own theme while running, so dark mode is applied here by
redefining the variables and overriding the few built-in widgets whose colours come from
the theme. Tables are drawn on a canvas that CSS cannot recolour, so they are inverted.
"""

from __future__ import annotations

LIGHT = dict(ink="#16213A", muted="#5B6784", line="#E3E8F2", bg="#FFFFFF",
             surface="#FFFFFF", surface2="#F5F7FC", navy="#3B5BDB", hover="#2F49B8",
             pill="#EDF1FA", tab_on="#FFFFFF", deep="#1B2A6B", hero2="#6C8CF5",
             g_bg="#DDF2E8", g_fg="#0B6B44", b_bg="#E3F1FB", b_fg="#1F5F8B",
             a_bg="#FBEFD2", a_fg="#7A5400", r_bg="#FDE3E2", r_fg="#9E1D1B",
             p_bg="#E0E7FF", p_fg="#1B2A6B", n_bg="#EEF1F7", n_fg="#586280",
             up="#128C5A", down="#D64545", dash="#C3CBDD")
DARK = dict(ink="#E8EDFA", muted="#9AA6C4", line="#2A3350", bg="#0C111F",
            surface="#121A2E", surface2="#1A2440", navy="#6C8CF5", hover="#8DA7FF",
            pill="#1C2644", tab_on="#FFFFFF", deep="#1B2A6B", hero2="#3B5BDB",
            g_bg="#13382A", g_fg="#6FD3A6", b_bg="#17303F", b_fg="#8FC3EA",
            a_bg="#3A2E0F", a_fg="#F0C35A", r_bg="#431C1B", r_fg="#FF9B98",
            p_bg="#222E5E", p_fg="#C7D4FF", n_bg="#1A2440", n_fg="#B0BAD6",
            up="#4FCB97", down="#FF7673", dash="#46527A")

# Line icons for furniture, 24x24, stroke only. Used as a faint outline row in the
# hero and, as a recolourable mask, beside section titles.
from urllib.parse import quote as _q

_ICONS = {
    "sofa": "<path d='M5 11V8a3 3 0 0 1 3-3h8a3 3 0 0 1 3 3v3'/><path d='M3 14a2 2 0 0 1 4 0v1h10v-1a2 2 0 0 1 4 0v4H3z'/>"
            "<path d='M6 18v2M18 18v2'/>",
    "chair": "<path d='M8 3h8l1 8H7z'/><path d='M6 11h12v3H6zM8 14v7M16 14v7'/>",
    "lamp": "<path d='M8 3h8l3 8H5z'/><path d='M12 11v8M8 21h8'/>",
    "bed": "<path d='M3 19V6M3 14h18v5M21 14v-2a3 3 0 0 0-3-3h-7v5'/><circle cx='7' cy='11' r='1.8'/><path d='M3 17h18'/>",
    "table": "<path d='M3 8h18M5 8v12M19 8v12M5 12h14'/><path d='M7 4h10l1 4H6z'/>",
}


def _svg(body: str, stroke: str = "black", w: int = 24, h: int = 24, vb: str = "0 0 24 24") -> str:
    return (f"<svg xmlns='http://www.w3.org/2000/svg' width='{w}' height='{h}' viewBox='{vb}' fill='none' "
            f"stroke='{stroke}' stroke-width='1.3' stroke-linecap='round' stroke-linejoin='round'>{body}</svg>")


def icon(name: str, stroke: str = "currentColor", size: int = 18) -> str:
    """An inline SVG for use in the app's own markup."""
    return _svg(_ICONS[name], stroke, size, size).replace("'", '"')


def _uri(svg: str) -> str:
    return "url(\"data:image/svg+xml," + _q(svg, safe="/:=,;") + "\")"


_ROW = "".join(f"<g transform='translate({k * 30} 2) scale(1.15)'>{_ICONS[n]}</g>"
               for k, n in enumerate(["sofa", "chair", "lamp", "bed", "table"]))
_HERO_ICONS = _uri(_svg(_ROW, "white", 150 * 2, 32 * 2, "0 0 150 32"))
_SEC_ICON = _uri(_svg(_ICONS["sofa"], "black"))
_ICON_CSS = (
    ".hero { position:relative; overflow:hidden; }"
    ".sec::before { content:''; display:inline-block; width:19px; height:19px; margin-right:.5rem; vertical-align:-3px;"
    f" background:var(--navy); -webkit-mask:{_SEC_ICON} center/contain no-repeat; mask:{_SEC_ICON} center/contain no-repeat; }}"
    ".brand .logo svg { display:block; }"
)
_BASE = """
@import url('https://fonts.googleapis.com/css2?family=Lora:wght@500;600;700&family=DM+Sans:wght@400;500;600;700&display=swap');
:root {{ --ink:{ink}; --muted:{muted}; --line:{line}; --bg:{bg}; --surface:{surface};
  --surface2:{surface2}; --navy:{navy}; --hover:{hover}; --pill:{pill}; --deep:{deep}; --hero2:{hero2};
  --g-bg:{g_bg}; --g-fg:{g_fg}; --b-bg:{b_bg}; --b-fg:{b_fg}; --a-bg:{a_bg}; --a-fg:{a_fg};
  --r-bg:{r_bg}; --r-fg:{r_fg}; --p-bg:{p_bg}; --p-fg:{p_fg}; --n-bg:{n_bg}; --n-fg:{n_fg};
  --up:{up}; --down:{down}; --dash:{dash};
  --serif:'Lora', Georgia, 'Times New Roman', serif; }}
html, body, [class*="css"], .stApp {{
  font-family: 'DM Sans', system-ui, -apple-system, 'Segoe UI', sans-serif;
  color: var(--ink); }}
h1, h2, h3, h4, .sec, .hero h1 {{ font-family: var(--serif) !important; }}
.stApp {{ background: var(--bg); }}
#MainMenu, footer, [data-testid="stToolbarActions"], [data-testid="stMainMenu"],
[data-testid="stAppDeployButton"], [data-testid="stDecoration"] {{ visibility:hidden; height:0; }}
[data-testid="stHeader"] {{ background: transparent; }}
/* The button that reopens a collapsed sidebar lives in the header; keep it visible. */
[data-testid="stExpandSidebarButton"], [data-testid="stSidebarCollapsedControl"] {{ visibility:visible !important; }}
[data-testid="stToolbar"] [data-testid="stBaseButton-header"], [data-testid="stToolbar"] a {{ display:none; }}
[data-testid="stExpandSidebarButton"], [data-testid="stSidebarCollapsedControl"] {{ color:var(--navy); }}
[data-testid="stExpandSidebarButton"] svg, [data-testid="stSidebarCollapsedControl"] svg {{ fill:var(--navy); color:var(--navy); }}
.block-container {{ padding: 1rem 2rem 3rem; max-width: 1500px; }}

[data-testid="stSidebar"] {{ background:var(--surface); border-right:1px solid var(--line); }}
[data-testid="stSidebar"] .block-container {{ padding: 1.4rem 1.1rem; }}
[data-testid="stSidebar"] h3 {{ font-family:'DM Sans',sans-serif !important; font-size:.72rem; letter-spacing:.09em; text-transform:uppercase;
  color:var(--muted); font-weight:700; margin:1.1rem 0 .35rem; }}
.brand {{ display:flex; gap:.6rem; align-items:center; margin-bottom:.4rem; }}
.brand .logo {{ background:var(--navy); color:#fff; width:34px; height:34px; border-radius:50%;
  display:flex; align-items:center; justify-content:center; font-weight:800; }}
.brand b {{ font-size:1rem; line-height:1.1; display:block; color:var(--ink); }}
.brand span {{ font-size:.72rem; color:var(--muted); }}

.hero {{ background:radial-gradient(circle at 94% 25%,rgba(255,255,255,.16) 0 54px,transparent 55px),radial-gradient(circle at 86% 90%,rgba(255,255,255,.10) 0 38px,transparent 39px),linear-gradient(120deg,var(--deep) 0%,var(--hero2) 100%); color:#fff;
  border-radius:12px; padding:.8rem 1.3rem; margin-bottom:.8rem;
  display:flex; justify-content:space-between; align-items:center; gap:.8rem; flex-wrap:wrap; }}
.hero h1 {{ font-size:1.5rem; margin:0; font-weight:700; letter-spacing:0; color:#fff !important; line-height:1.15; }}
.hero p {{ margin:.1rem 0 0; opacity:.8; font-size:.82rem; color:#fff !important; }}
.chips {{ display:flex; gap:.35rem; flex-wrap:wrap; }}
.chip {{ background:rgba(255,255,255,.14); border:1px solid rgba(255,255,255,.26); color:#fff;
  padding:.18rem .6rem; border-radius:999px; font-size:.72rem; font-weight:600; white-space:nowrap; }}
.chip.ok {{ border-color:rgba(160,235,200,.75); }}
.chip.warn {{ border-color:rgba(255,215,120,.85); }}
.chip.bad {{ border-color:rgba(255,183,195,.85); }}

[role="tablist"] {{ gap:.3rem; background:var(--surface); padding:.35rem; border-radius:12px;
  border:1px solid var(--line); width:fit-content; max-width:100%; flex-wrap:wrap; }}
[data-testid="stTab"] {{ height:38px; border-radius:9px; padding:0 1.15rem; margin:0; font-weight:600;
  color:var(--muted); background:transparent; }}
[data-testid="stTab"][aria-selected="true"] {{ background:var(--navy); color:#fff !important; }}
[data-testid="stTab"][aria-selected="true"] p {{ color:#fff !important; }}
[data-testid="stTabs"] [data-testid="stTabHighlight"], [data-testid="stTabs"] [data-testid="stTabBorder"] {{ display:none; }}
[data-testid="stTabPanel"] {{ padding-top:1.1rem; }}

/* the second level of tabs (inside a group) reads as sub-navigation: underline, not a pill bar */
[data-testid="stTabPanel"] [role="tablist"] {{ background:transparent; border:0; border-bottom:1px solid var(--line);
  border-radius:0; padding:0; gap:1.5rem; width:100%; }}
[data-testid="stTabPanel"] [data-testid="stTab"] {{ height:36px; padding:0 .15rem; border-radius:0; background:transparent !important;
  border-bottom:2px solid transparent; font-weight:500; }}
[data-testid="stTabPanel"] [data-testid="stTab"][aria-selected="true"] {{ background:transparent !important;
  border-bottom-color:var(--navy); }}
[data-testid="stTabPanel"] [data-testid="stTab"][aria-selected="true"] p {{ color:var(--ink) !important; font-weight:700; }}
[data-testid="stTabPanel"] [data-testid="stTabPanel"] {{ padding-top:1rem; }}

[data-testid="stVerticalBlockBorderWrapper"] {{ background:var(--surface); border-radius:14px;
  border:1px solid var(--line) !important; box-shadow:0 1px 2px rgba(16,24,40,.04); }}
h2, h3, h4 {{ color:var(--ink); letter-spacing:-.01em; }}
.sec {{ font-size:1.02rem; font-weight:700; margin:.1rem 0 .15rem; color:var(--ink); }}
.sub {{ color:var(--muted); font-size:.86rem; margin-bottom:.7rem; }}

.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:.8rem; margin:.2rem 0 1rem; }}
.tiles.tight {{ grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); }}
.tile {{ background:var(--surface); border:1px solid var(--line); border-radius:14px; padding:.85rem 1rem;
  box-shadow:0 1px 2px rgba(16,24,40,.04); }}
.tile .l {{ font-size:.72rem; text-transform:uppercase; letter-spacing:.07em; color:var(--muted); font-weight:700; }}
.tile .v {{ font-size:1.55rem; font-weight:760; margin:.15rem 0; letter-spacing:-.02em; color:var(--ink); }}
.tile .s {{ font-size:.78rem; color:var(--muted); display:flex; gap:.4rem; align-items:center; flex-wrap:wrap; }}
.tag {{ padding:.1rem .5rem; border-radius:999px; font-size:.7rem; font-weight:700; }}
.t-reported {{ background:var(--g-bg); color:var(--g-fg); }} .t-derived {{ background:var(--b-bg); color:var(--b-fg); }}
.t-estimated {{ background:var(--a-bg); color:var(--a-fg); }} .t-assumed {{ background:var(--r-bg); color:var(--r-fg); }}
.t-unavailable, .t-none {{ background:var(--n-bg); color:var(--n-fg); }} .t-calibrated {{ background:var(--p-bg); color:var(--p-fg); }}
.t-call_stated {{ background:var(--b-bg); color:var(--b-fg); }}
.up {{ color:var(--up); }} .down {{ color:var(--down); }}

.row {{ display:flex; justify-content:space-between; align-items:center; gap:.8rem; padding:.5rem 0;
  border-bottom:1px solid var(--line); font-size:.9rem; color:var(--ink); }}
.row:last-child {{ border-bottom:0; }}
.row .name {{ color:var(--muted); font-size:.78rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:55%; }}
.pill {{ padding:.12rem .6rem; border-radius:999px; font-size:.74rem; font-weight:700; white-space:nowrap; }}
.pill.ok {{ background:var(--g-bg); color:var(--g-fg); }} .pill.no {{ background:var(--pill); color:var(--muted); }}
.pill.err {{ background:var(--r-bg); color:var(--r-fg); }} .pill.warn {{ background:var(--a-bg); color:var(--a-fg); }}

.stButton > button, .stDownloadButton > button {{ border-radius:10px; font-weight:650; border:1px solid var(--line); }}
.stButton > button[kind="primary"] {{ background:var(--navy); border-color:var(--navy); color:#fff; }}
.stButton > button[kind="primary"]:hover {{ background:var(--hover); border-color:var(--hover); }}
[data-testid="stFileUploaderDropzone"] {{ border:1.5px dashed var(--dash); border-radius:12px; background:var(--surface2); }}
[data-testid="stDataFrame"] {{ border:1px solid var(--line); border-radius:12px; overflow:hidden; }}
[data-testid="stExpander"] {{ border:1px solid var(--line); border-radius:12px; background:var(--surface); }}
.stAlert {{ border-radius:10px; padding:.15rem .25rem; }}
[data-testid="stExpander"] summary {{ font-weight:600; }}
[data-testid="stExpander"] summary:hover {{ background:var(--pill); }}
[data-testid="stExpander"] {{ margin-bottom:.45rem; }}
"""

_DARK_ONLY = """
.stApp, [data-testid="stSidebar"] {{ color-scheme: dark; }}
[data-testid="stMarkdownContainer"] p, [data-testid="stMarkdownContainer"] li,
[data-testid="stWidgetLabel"] p, label, [data-testid="stCaptionContainer"],
[data-testid="stRadio"] p, [data-testid="stToggle"] p, [data-testid="stExpander"] summary p,
[data-testid="stExpander"] summary span, h1, h2, h3, h4 {{ color:var(--ink) !important; }}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {{ color:var(--muted) !important; }}
.hero *, .chip {{ color:#fff !important; }}
.tag, .pill {{ color:inherit; }}
.t-reported, .pill.ok {{ color:var(--g-fg) !important; }} .t-derived, .t-call_stated {{ color:var(--b-fg) !important; }}
.t-estimated, .pill.warn {{ color:var(--a-fg) !important; }} .t-assumed, .pill.err {{ color:var(--r-fg) !important; }}
.t-unavailable, .t-none {{ color:var(--n-fg) !important; }} .t-calibrated {{ color:var(--p-fg) !important; }}
.pill.no {{ color:var(--muted) !important; }}
[data-baseweb="select"] > div, [data-baseweb="input"], [data-baseweb="base-input"],
[data-baseweb="textarea"], input, textarea {{ background:var(--surface2) !important;
  color:var(--ink) !important; border-color:var(--line) !important; }}
[data-baseweb="select"] div {{ background-color:var(--surface2) !important; color:var(--ink) !important; }}
[data-baseweb="select"] > div, [data-baseweb="select"] > div > div, [data-baseweb="select"] > div > div > div,
[data-baseweb="input"] > div, [data-baseweb="base-input"] > div {{ background-color:var(--surface2) !important;
  border-color:var(--line) !important; box-shadow:none !important; }}
[data-testid="stSelectbox"] [data-baseweb="select"], [data-testid="stTextInput"] [data-baseweb="input"],
[data-testid="stNumberInput"] [data-baseweb="input"], [data-testid="stSelectbox"] [data-baseweb="select"] > div:first-child,
[data-testid="stMultiSelect"] [data-baseweb="select"] > div:first-child {{ border:1px solid var(--line) !important;
  outline:0 !important; box-shadow:none !important; background:var(--surface2) !important; }}
[data-testid="stSelectbox"] div, [data-testid="stMultiSelect"] div {{ background-color:var(--surface2); border-color:var(--line) !important; }}
[data-testid="stSelectbox"] [data-testid="stWidgetLabel"], [data-testid="stSelectbox"] [data-testid="stWidgetLabel"] div,
[data-testid="stMultiSelect"] [data-testid="stWidgetLabel"], [data-testid="stMultiSelect"] [data-testid="stWidgetLabel"] div {{ background:transparent !important; }}
[data-testid="stMultiSelect"] span[data-baseweb="tag"] {{ background-color:var(--navy) !important; }}
input::placeholder, textarea::placeholder {{ color:var(--muted) !important; opacity:1; }}
[data-testid="stExpander"] details {{ border:1px solid var(--line) !important; border-radius:12px; }}
[data-baseweb="select"] svg, [data-testid="stSelectbox"] svg {{ fill:var(--muted) !important; }}
[data-testid="stSelectbox"] .group, [data-testid="stSelectbox"] .group input {{ background-color:var(--surface2) !important; color:var(--ink) !important; }}
[data-baseweb="radio"] > div:first-child {{ background:var(--surface2) !important; border-color:var(--muted) !important; }}
[data-testid="stRadioOption"] > div > div:first-child {{ background-color:var(--surface2) !important;
  border-color:var(--muted) !important; }}
[data-testid="stRadioOption"][data-selected="true"] > div > div:first-child {{ background-color:var(--navy) !important;
  border-color:var(--navy) !important; }}
[data-testid="stRadioOption"][data-selected="true"] > div > div:first-child > div {{ background-color:var(--on, #fff) !important; }}
[data-testid="stCheckbox"] label > div:first-of-type {{ background-color:var(--surface2) !important; border-color:var(--muted) !important; }}
[data-testid="stCheckbox"][data-selected="true"] label > div:first-of-type {{ background-color:var(--navy) !important; border-color:var(--navy) !important; }}
button[data-testid="stBaseButton-secondary"], button[data-testid="stBaseButton-secondary"]:disabled {{
  background-color:var(--surface2) !important; color:var(--ink) !important; border:1px solid var(--line) !important; }}
button[data-testid="stBaseButton-secondary"]:disabled {{ opacity:.55; }}
button[data-testid="stBaseButton-secondary"]:hover:not(:disabled) {{ border-color:var(--navy) !important; color:var(--navy) !important; }}
[data-testid="stAlertContainer"] {{ background:var(--surface2) !important; border:1px solid var(--line); color:var(--ink) !important; }}
[data-testid="stAlertContainer"] p, [data-testid="stAlertContainer"] li, [data-testid="stAlertContainer"] div {{ color:inherit !important; }}
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentSuccess"]) {{ background:var(--g-bg) !important; color:var(--g-fg) !important; border-color:var(--g-fg); }}
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentWarning"]) {{ background:var(--a-bg) !important; color:var(--a-fg) !important; border-color:var(--a-fg); }}
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentError"]) {{ background:var(--r-bg) !important; color:var(--r-fg) !important; border-color:var(--r-fg); }}
[data-baseweb="popover"] ul, [data-baseweb="menu"], [role="listbox"] {{ background:var(--surface2) !important; }}
[data-baseweb="popover"] li, [role="option"] {{ color:var(--ink) !important; }}
[role="option"]:hover, [data-baseweb="popover"] li:hover {{ background:var(--line) !important; }}
[data-testid="stFileUploaderDropzoneInstructions"], [data-testid="stFileUploaderDropzoneInstructions"] * {{ color:var(--muted) !important; }}
[data-testid="stFileUploaderFile"], [data-testid="stFileUploaderFile"] * {{ color:#1A1A1A !important; }}
[data-testid="stFileUploaderDropzone"] button {{ background:var(--surface) !important; border-color:var(--line) !important; }}
.stButton > button:not([kind="primary"]), .stDownloadButton > button {{ background:var(--surface2);
  color:var(--ink); }}
.stButton > button[kind="primary"] p {{ color:#fff !important; }}
[data-testid="stDataFrame"] {{ filter: invert(0.93) hue-rotate(180deg); }}
[data-testid="stExpander"] details, [data-testid="stExpander"] summary {{ background:var(--surface); color:var(--ink); }}
[data-testid="stJson"], pre, code {{ background:var(--surface2) !important; color:var(--ink) !important; }}
[data-testid="stSidebar"] hr {{ border-color:var(--line); }}
"""


def _pattern(stroke: str, opacity: float) -> str:
    """A tile of scattered product outlines, repeated over the whole page behind the content."""
    spots = [("sofa", 14, 18, -12, 1.7), ("chair", 150, 8, 14, 1.6), ("table", 236, 70, -8, 1.6),
             ("lamp", 64, 120, 10, 1.7), ("bed", 188, 150, -16, 1.6), ("sofa", 8, 232, 18, 1.5),
             ("bed", 120, 238, -6, 1.5), ("chair", 250, 214, 8, 1.5)]
    body = "".join(f"<g transform='translate({x} {y}) rotate({r} 12 12) scale({s})'>{_ICONS[n]}</g>"
                   for n, x, y, r, s in spots)
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' width='300' height='300' viewBox='0 0 300 300' fill='none' "
           f"stroke='{stroke}' stroke-opacity='{opacity}' stroke-width='1' stroke-linecap='round' "
           f"stroke-linejoin='round'>{body}</svg>")
    return _uri(svg)


def _page_css(dark: bool) -> str:
    tile = _pattern("%23F27BAA" if dark else "%23D63F7E", 0.09 if dark else 0.07).replace("%2523", "%23")
    return (f".stApp {{ background-image:{tile}; background-size:380px 380px; background-attachment:fixed; }}"
            f"[data-testid='stSidebar'] {{ background-image:{tile}; background-size:380px 380px; }}"
            "[data-testid='stVerticalBlockBorderWrapper'], .tile, [role='tablist'] { background:color-mix(in srgb, var(--surface) 95%, transparent); }")


def css(dark: bool = False) -> str:
    """The full stylesheet, as a <style> block."""
    vars_ = DARK if dark else LIGHT
    text = _BASE.format(**vars_) + (_DARK_ONLY.format() if dark else "") + _ICON_CSS + _page_css(dark)
    return f"<style>{text}</style>"
