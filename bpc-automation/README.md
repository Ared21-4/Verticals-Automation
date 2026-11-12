# BPC Player Intelligence, Estimation and Calibration

A monthly pipeline that turns a BPC player's MIS, an expert call transcript and the reference
workbook into the player's 178-row upload sheet, plus a full audit trail. It is the Fashion
portal's engine with BPC configuration, so the two work the same way.

**Eleven BPC players**, listed in `bpc_intel/config/bpc_players.json`: Nykaa, Purplle, Mamaearth,
Sugar, Mcaffeine, Beardo, Plum, Sephora, WOW, MAC, MyGlamm.

## Run the portal

Double-click `Start Portal.bat`, or:

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m streamlit run app.py
```

Tabs: **Upload → Weights → Validate & run → Review → History → Collate**. Pick the player and the
month, drop in the MIS and the call, run, review, download. "Several players" mode runs any set of
players at once and zips the results.

## Run from the command line

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
& $py run_cli.py --list-players --month 2026-09
& $py run_cli.py --player nykaa --month 2026-09 `
    --mis "Nykaa MIS.xlsx" --transcript "Nykaa_BPC_September_Expert_Call_Insights.docx"
& $py run_cli.py --player sugar --month 2026-09          # nothing supplied: built from history
& $py run_cli.py --all-players --month 2026-09
```

## Inputs

| Input | What it is |
|---|---|
| **Reference** | The master BPC workbook (or the slim copy). Optional: `reference/BPC Reference.xlsx` is built in and is used when nothing is uploaded. An uploaded reference wins. Files are recognised by their contents, not their names. |
| **MIS** | The standard BPC template: Overall, Category GMV, Category ASP, Payment Split, Geographic Split, Price Buckets. Nine category lines (Body Skin, Face Skin, Fragrance, Hair Care, Hair Care Others, Hair Colour, Hygiene, Make up, Others). The unit (rupees or crore) is detected against the player's own history; a zero is read as "not supplied". A MIS whose level is far off the player's last month (wrong player, month or unit) stops the run. |
| **Call** | The expert-call write-up (.docx). Used for stated figures and for detecting that a sale event moved out of the month. |

With neither a MIS nor a call, the whole sheet is still built from the player's own history,
seasonality and remaining evidence, every value marked as an estimate.

## What comes out

`outputs/<Player> <Mon Year>/`:

| File | What it is |
|---|---|
| `... - Output.xlsx` | **The deliverable.** `BPC-WF`: the 178 upload parameters in their delivered order (`parent_question, parameter_name, par_id, unit, <month>`), then an "Additional" block (shipped GMV, net GMV, returns, ASP, basket size, category shipped GMV). Plus `Contents`, `Source`, `Key`. |
| `... - Audit.xlsx` | Inputs, validation, weights, call insights, caveats, every value with provenance, corroboration, calibration, backtests, adjustments, overrides. |
| `... - Model Column.xlsx` | The month's column in model-sheet order, for deliberate paste-in. |
| `... - Analysis.xlsx` | Trend and deviation view. |

The reference workbook is **never modified**. Inputs, manifest and weights are kept under
`data/runs/<run_id>/`.

## How BPC differs from Fashion

- **Categories.** The MIS lists nine lines; the output carries three: Grooming (personal care),
  Makeup & Fragrance, Others. Lines roll up: GMV adds, ASP is combined GMV over combined units.
  Their shares are 1580 / 1581 / 1583, ASPs 492 / 493 / 494, GMV 249 / 250 / 4571.
- **Rows without a par_id.** BPC sheets leave Shipped GMV, Returned GMV, Net GMV, Gross
  Transactions, Basketsize and the category GMV rows without one. `config/label_pars.json` binds them by
  label (first occurrence only), so they get history and can be estimated.
- **Category-level rows mirror the platform.** "Beauty & Personal Care" MTU, payment and city-tier
  rows (3612, 4487, 4499, 4511, 4523, 4535) repeat the platform rows (238, 287, 286, 476, 477, 478).
- **AOV** is estimated from its own series (or taken from the MIS when it agrees with history).
  Fashion rebuilds it as ASP x basket; BPC sheets' ASP and basket rows are too loosely kept for that.
- **Anchor.** Gross GMV anchors; shipped GMV is derived from cancellation.

Metrics are addressed by `par_id`; one the sheet does not carry is reported as unavailable, never
invented. History balance, trend and deviation, weights, saved weight sets and month-by-month
history all work as in the Fashion portal (see `METHODOLOGY.md`).

## Month to month

1. Pick the player and the new month. 2. Upload the MIS and call. 3. Run, review, download.
4. *Save this month to history*; the next month builds on it. Months must be run in order.

The only prerequisite is that the player's sheet has a column for the month. Validation stops the
run and says so rather than guessing.

### Adding a player

Add a line to `bpc_players.json` (name, exact sheet name, aliases) and, for depth beyond the shared
profile, a `config/players/<key>.json` with only what differs.

## Known data points to watch

- **MyGlamm** has zeros for GMV in recent months, so it has no usable level; expect a flagged,
  low-confidence run until its sheet is updated.
- **MAC** carries `#DIV/0!` and a few implausible rows (basket size, MTU) in the latest months.
- Several players' latest reference months are placeholders carried forward, not actuals.
- Players with no "Others" share row (Plum, Sugar, Mcaffeine) get that category from the MIS only.

## Tests

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m pytest tests -q
```

36 tests (about 15 seconds): discovery, label binding, the MIS roll-up, a Nykaa run with MIS and
call, upload-sheet order, all 11 players with no files, and the wrong-player guard.
