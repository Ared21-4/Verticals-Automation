# Furniture Player Intelligence, Estimation and Calibration

A monthly pipeline that turns a furniture player's MIS, an expert call transcript and the master
workbook into the player's upload sheet plus a full audit trail. It is the Fashion and BPC portals'
engine with furniture configuration, so all three work the same way.

**Two players**, listed in `furniture_intel/config/furniture_players.json`: Pepperfry and Urban Ladder.

## Run the portal

Double-click `Start Portal.bat`, or:

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m streamlit run app.py
```

Tabs: **Upload → Weights → Validate & run → Review → History → Collate → Edited Analysis**. Pick the
player and the month, drop in the MIS and the call, run, review, download. "Several players" mode
runs both and zips the results.

## Run from the command line

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
& $py run_cli.py --list-players --month 2026-10
& $py run_cli.py --player pepperfry --month 2026-10 `
    --reference "Furniture_Output_for automation.xlsx" `
    --mis "Pepperfry MIS.xlsx" --transcript "Pepperfry_October_Expert_Call_Insights.docx"
& $py run_cli.py --player urban_ladder --month 2026-10      # nothing supplied: built from history
& $py run_cli.py --all-players --month 2026-10
```

## Inputs

| Input | What it is |
|---|---|
| **Reference** | The master workbook (`Furniture_Output_for automation.xlsx`): tabs `Pepperfry Automated` and `Urbanladder Automated`. Upload it each month; it wins. `reference/Furniture Reference.xlsx` is a built-in fallback of the same two tabs. Files are recognised by their contents, not their names. |
| **MIS** | The generic MIS template: Overall, Category GMV, Category ASP, Payment Split, Geographic Split. Category lines: Home furnishings, Furniture, Home Decor, Home improvement tools, Household. The unit (rupees or crore) is detected against the player's own history; a zero is read as "not supplied". A MIS whose level is outside 0.6x-1.6x of last month (wrong player, month or unit) stops the run. |
| **Call** | The expert-call write-up (.docx). Used for stated figures and for detecting that a sale event moved out of the month. |

With neither a MIS nor a call, the whole sheet is still built from the player's own history,
seasonality and remaining evidence, every value marked as an estimate.

## What comes out

`outputs/<Player> <Mon Year>/`:

| File | What it is |
|---|---|
| `... - Output.xlsx` | **The deliverable.** `Furniture-WF`: the 67 rows of the Furniture Output layout (`parent_question, parameter_name, par_id, unit, <month>`), percentages as fractions. Plus `Contents`, `Source`, `Key`. |
| `... - Audit.xlsx` | Inputs, validation, weights, call insights, caveats, every value with provenance, corroboration, calibration, backtests, adjustments, overrides. |
| `... - Model Column.xlsx` | The month's column in model-sheet order, for deliberate paste-in. |
| `... - Analysis.xlsx` | Trend and deviation view. |
| `... - Channel Distribution.xlsx` | **Pepperfry only.** Your `Pepperfry Channel Distribution` sheet itself, with the month added: same formats, hidden columns, formulas and the Working / Total / Contribution / Output / Check blocks. See below. |

The reference workbook is **never modified**. Inputs, manifest and weights are kept under
`data/runs/<run_id>/`.

## How furniture differs

- **Identities** follow the sheets' own formulas (see `METHODOLOGY.md`): returned GMV on gross for
  Pepperfry and on shipped for Urban Ladder; items = GMV x 10^7 / ASP; orders per day from AOV and the
  month's length; gross transactions as a count with MTU = transactions / 10^6.
- **Rows with no par_id or conflicting par_ids** are fixed by `config/forced_pars.json` and
  `config/label_pars.json`.
- **History** is read from Apr'21: before May'20 the sheets interleave the two players in alternate
  columns.

## Channel Distribution (Pepperfry)

Each Pepperfry run also writes the channel sheet, a copy of your own with the new month inserted in
the table and in each of the five blocks. Shipped GMV and the Output block take this run's figures;
the online share is the last three months' mean; the studio and online category mixes are last
month's, moved the least needed so that online x mix + studio x mix reproduces this run's category
shares (the Check block then reads 0). Rows 16 and 26 stay the sheet's own `1 - SUM(...)` formulas.
These are estimates, not reported channel figures. Supply real ones by overwriting the input cells in the
output, or set `channel_distribution.override` (`online`, `on`, `off`) in `players/pepperfry.json`.

It continues from, in order of preference, the sheet in the uploaded master, the copy saved by the
last *Save to history*, or the built-in `reference/Pepperfry Channel Distribution.xlsx`, whichever
ends at the month before. It refuses a gap. Cross-sheet links (Shipped GMV, Output) are written as
values because the other tabs are not carried; formulas recalculate when Excel opens the file.
Urban Ladder has no channel sheet.

## Month to month

1. Pick the player and the new month. 2. Upload the master workbook, MIS and call. 3. Run, review,
download. 4. *Save this month to history*; the next month builds on it. Months must be run in order.

The only prerequisite is that the player's sheet has a column for the month. Validation stops the run
and says so rather than guessing.

### Adding a player

Add a line to `furniture_players.json` (name, exact sheet name, `start_month`, aliases) and, for depth
beyond the shared profile, a `config/players/<key>.json` with only what differs (e.g. `returns_base`).

## Known data points to watch

- Gross GMV is a formula, not a typed figure, since May'25 (Pepperfry) and Dec'25 (Urban Ladder). The workbook does not say whether those months are actuals, so confirm before relying on a history-only run.
- Pepperfry has no unique-visitor, Fulfilled+ or Tier-I history; those rows export as null.
- Urban Ladder's Home improvement tools and Household have been blank for months, so they export as null.

## Tests

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m pytest tests -q
```

33 tests (about 30 seconds): discovery and start month, par binding on both sheets, the sheets'
identities, split totals, a Pepperfry run with a MIS, the wrong-player guard, output order and the
edited-analysis rebuild, and the channel workbook (reconciliation, format kept, formulas still on their own month, chaining, gap refused).
