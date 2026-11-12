# Fashion Player Intelligence, Estimation and Calibration

A monthly pipeline that turns a player MIS, an expert call transcript and the
reference workbook into the player's upload-format output sheet, plus a full
audit trail.

**Thirteen fashion players**, listed in `fashion_intel/config/fashion_players.json`:
Myntra, Ajio, Zivame, H&M, M&S, Clovia, Bewakoof, Limeroad, Nykaa Fashion,
Shoppers Stop, Zara, Snapdeal, Tata CLiQ.

Snapdeal and Tata CLiQ keep their history in their own workbooks (Snapdeal_product.xlsx,
Tata Cliq_product.xlsx): the SNAPDEAL and Tatacliq sheets, with the months straight after the
parameter name and no units column. Either workbook can be uploaded as the reference; each player
uses the workbook that holds it and the built-in history otherwise. Their history is also in the
built-in copy, and the output uses the same template and parameters as every other player.

**Reference.** Upload the reference workbook with each month's files (the master
workbook, or the slim copy). It carries the previous numbers, so the trend and
deviation view and the seasonal history follow your latest figures; it takes
precedence over anything saved by the app. If none is uploaded, a built-in copy
(`reference/Fashion Reference.xlsx`, 2.2 MB) is the fallback. Files are recognised
by their contents, not their names. Nothing else is required: with no MIS or call, the whole
output — platform GMV, shipped, net, AOV, ASP, the full Fashion/Apparel category
tree, shares, splits, user and traffic metrics — is built from the player's own
history, seasonality and the remaining evidence.

**Dark mode.** A toggle in the portal sidebar switches the whole page. It is applied by
the app's own stylesheet because Streamlit cannot change its theme while running.

**History balance.** The output is not just the MIS figure. It is moved toward the level whose
MoM and YoY, on your stored reference, best match what the MIS states, so neither is far off the
other. A **History weight** slider in the sidebar sets how far (100% default, 0% = the MIS
unchanged). The MIS figure is always shown beside the output, history is saved as the MIS reports
it, and the weight is learned from earlier months, so the gap narrows. See the `Balance` sheet.

**Trend and deviation.** The Review tab and a `Trend` sheet in the audit workbook set each
headline metric against last month, the same month last year, the recent average, and the
figure the reference already held for this month, flagging large moves.

**MIS format.** Myntra has its own tuned reader. Every other player sends the
standard fashion template (Overall, Category GMV, Category ASP, Payment Split,
Geographic Split, Price Buckets). The reader detects the unit by comparing the
MIS with the player's own history (rupees, lakh, million or crore), treats a zero
as "not supplied", maps every category by `par_id`, and reports each decision in
the findings. A MIS whose level is more than 5x off last month's (the wrong
player, month or unit) stops the run instead of producing numbers.

**Month to month.** After a run, *Save this month to history* stores it, so the
next month builds on it. Months must be run in order; skipping one is refused.

Metrics are addressed by **`par_id`, not row number**, because row layouts
differ per player while par_ids do not. A metric a player's sheet does not
carry is reported as unavailable, never invented.

| | Myntra | Every other player |
|---|---|---|
| Config | Tuned: MIS taxonomy, L2/L3 tree, corroboration rules | Shared fashion profile |
| Anchor | Shipped GMV from the MIS | Gross GMV; shipped derived from cancellation |
| Categories | Full tree from the MIS | Mix from history, scaled onto the platform total |
| Metrics bound | 208 | 50–109 |

Adding a MIS mapping for another player upgrades it to Myntra's depth with no
code change.

---

## Setup

Requires Python 3.11+.

```powershell
python -m pip install -r requirements.txt
```

On the machine this was built on, Python was installed user-scoped with
`winget install --id Python.Python.3.12 --scope user`. The interpreter lands at
`%LOCALAPPDATA%\Programs\Python\Python312\python.exe` and is not on `PATH`, so
the commands below use it directly.

## Run the portal

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m streamlit run app.py
```

Then work through the five tabs: **Upload → Weights → Validate & run → Review →
History**.

## Run from the command line

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"

& $py run_cli.py --list-players --month 2026-09          # the 13 players

# one player, with its MIS and call
& $py run_cli.py --player myntra --month 2026-09 `
    --mis "Myntra MIS.xlsx" --transcript "Myntra_September_Expert_Call_Insights.docx"

# one player, nothing supplied: built from history alone
& $py run_cli.py --player zara --month 2026-09

# every player in one pass; add --save-history to keep each month
& $py run_cli.py --all-players --month 2026-09
```

Optional: `--traffic`, `--logistics`, `--social`, `--out`, `--seasonality`,
`--save-history`. `--reference` is only needed to use a different workbook.

Each player writes to its own folder, e.g. `outputs\H&M Sep 2026\`.

## Tests

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m pytest tests -q
```

173 tests (about 40 seconds): engine units, the 13 acceptance criteria against
the real Myntra files, multi-player runs, and the built-in reference, history
and no-MIS-no-call paths for all 13 players.

---

## What comes out

Each run writes three workbooks to `outputs/<run_id>/`:

| File | What it is |
|---|---|
| `<Player> <Mon Year> - Output.xlsx` | **The deliverable.** The upload sheet in the template's five columns, ordered platform topline → category shipped GMV → category ASP → category contribution → other calculated metrics, with unavailable parameters grouped at the bottom. Plus `Contents`, `Source` and `Key` sheets |
| `<Player> <Mon Year> - Audit.xlsx` | 14 sheets: inputs, validation, weights, call insights, caveats, every value with provenance, corroboration, calibration, backtests, adjustments, method changes, overrides, key |
| `<Player> <Mon Year> - Model Column.xlsx` | The month's column in model-sheet row order, for deliberate paste-in |

Output order is set per player under `output_layout` in the config, so it can
be changed without touching code.

The reference workbook is **never modified in place**.

Inputs, manifest, weights in force, findings and overrides are retained under
`data/runs/<run_id>/`, so a month can be reproduced or audited later.

---

## Next month, without touching code

1. Open the portal, pick the player and the new month.
2. Upload that month's MIS and transcript. The reference workbook is only needed
   when its history or format changes.
3. Weights: the set last saved for that player loads automatically. Edit if
   required and save.
4. Run, review, download. Repeat per player, or use `--all-players`.

The only prerequisite is that the player's sheet has a column for the new month.
If it does not, validation stops the run and says so rather than guessing — four
of the supplied players stop at May'24 and are blocked for Sep'26 on exactly
that basis.

**Weights apply every run.** Configured weights are standing policy; effective
weights are what a run used after removing missing sources and discounting weak
ones. Both are written into the audit workbook, so each month records the
weighting that produced it.

### Adding another player

**Nothing to do** if the player already has a sheet in the reference workbook —
it is discovered and appears in the portal. It runs on the shared fashion
profile in `config/base_fashion.json`.

To give a player Myntra's depth, add `config/players/<key>.json` with only what
differs: `mis_taxonomy_map` (MIS category → model category), `l2_categories`,
`segments`, `corroboration` rules. The base profile supplies everything else.
Keys are lowercased and underscored: `Marks & Spencer` → `marks_spencer.json`.

### How a player's config is built

```
config/base_fashion.json      shared profile, metrics addressed by par_id
  + config/players/<key>.json only what this player does differently
  + the player's own sheet     par_ids resolved to that sheet's row numbers
```

---

## Layout

```
fashion_intel/
  domain.py              Value + Provenance — every number carries its origin
  discovery.py           finds the players a reference workbook contains
  config_resolver.py     base profile + player override + bind par_ids to rows
  pipeline.py            run_month(): the whole product, UI-independent
  store.py               runs, manifests, overrides, saved weight sets
  config/
    base_fashion.json    shared profile: metrics by par_id, bounds, layout
    weights_default.json
    players/myntra.json  MIS taxonomy, category tree, corroboration rules
  readers/
    reference.py         model sheet + output template
    mis.py               MIS sections (section-scoped: names repeat)
    transcript.py        deterministic .docx extraction + event-shift detection
  engine/
    weights.py           availability, quality discount, renormalisation
    estimators.py        seasonal-naive estimator + walk-forward backtest
    validate.py          pre-flight checks; errors block, warnings carry through
    estimate.py          builds the month's full metric column
    calibrate.py         implied historical error + measured accuracy
  export/workbook.py     deliverable, audit pack, model column
app.py                   Streamlit portal
run_cli.py               command-line runner
tests/                   64 tests
```

`METHODOLOGY.md` documents the calculation rules, the weighting scheme, the
calibration approach, the decisions taken and the known limitations.

## Update: several players, growth controls, brand-level weight
- **Several players mode** (sidebar): run any set of players at once (default: Bewakoof, H&M, Limeroad, Clovia, M&S, Shoppers Stop, Zara, Zivame) or one player alone. Files are matched to players by name; a player with no files runs on history. Output: one folder per player plus "All players <Mon YYYY>.zip".
- **MoM / YoY on Review**: tiles show YoY and MoM for gross, shipped, net and AOV. The Growth card lets you change MoM and YoY and recalculate; every figure built from gross GMV follows.
- **Brand-level data weight**: added at 0% (total stays 100%). Set it in Weights; it counts as a source only when a brand-level file is supplied.