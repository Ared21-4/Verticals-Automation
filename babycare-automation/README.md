# Babycare Player Intelligence, Estimation and Calibration

A monthly pipeline that turns Firstcry's MIS, an expert call transcript and the master workbook into the
upload sheet (the Firstcry Webform layout) plus a full audit trail. It is the Fashion, BPC and Furniture
portals' engine with babycare configuration, so all four work the same way.

**One player**, listed in `babycare_intel/config/babycare_players.json`: Firstcry (sheet `Revised Babycare`).

## Run the portal

Double-click `Start Portal.bat`, or:

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m streamlit run app.py
```

Tabs: **Upload → Weights → Validate & run → Review → History → Collate → Edited Analysis**.

## Run from the command line

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
& $py run_cli.py --list-players --month 2026-10
& $py run_cli.py --player firstcry --month 2026-10 `
    --reference "Babycare Working file+Output.xlsx" `
    --mis "Firstcry MIS.xlsx" --transcript "Firstcry_October_Expert_Call_Insights.docx"
& $py run_cli.py --player firstcry --month 2026-10      # nothing supplied: built from history
```

## Inputs

| Input | What it is |
|---|---|
| **Reference** | The master workbook; the `Revised Babycare` tab is read (par_id in column A, months from Jan'21 on row 5). Upload it each month; it wins. `reference/Babycare Reference.xlsx` is a built-in values-only fallback of that tab, ending Sep'26 with an empty Oct'26 column. |
| **MIS** | The generic MIS template: Overall, Category GMV, Category ASP, Payment Split, Geographic Split. Category lines: Kids Fashion, Diaper & Nappies, Baby Care Supplies, Baby Gear, Others. Unit (rupees or crore) is detected against history; a zero is read as "not supplied". A MIS outside 0.6x-1.6x of last month stops the run. |
| **Call** | The expert-call write-up (.docx): stated figures, and detection of a sale event moving out of the month. |

With neither a MIS nor a call the whole sheet is still built from the player's history and seasonality,
every value marked as an estimate.

## What comes out

`outputs/<Player> <Mon Year>/`:

| File | What it is |
|---|---|
| `... - Output.xlsx` | **The deliverable.** `Babycare-WF`: the 86 rows of the Firstcry Webform layout (`parent_question, parameter_name, par_id, unit, <month>`), percentages as fractions. Plus `Contents`, `Source`, `Key`. |
| `... - Audit.xlsx` | Inputs, validation, weights, call insights, caveats, every value with provenance, corroboration, calibration, backtests, adjustments, overrides. |
| `... - Model Column.xlsx` | The month's column in sheet order, for deliberate paste-in. |
| `... - Analysis.xlsx` | Trend and deviation view. |

The reference workbook is **never modified**. Inputs, manifest and weights are kept under `data/runs/<run_id>/`.

## What the 86 rows are

| Block | Rows | Source |
|---|---|---|
| City split, payment, shipment split, topline, category GMV and ASP | 21 | Firstcry's own rows, estimated or MIS |
| BPC and Home & Furniture (GMV, ASP, transactions, MTU, cancelled, payment, tier, RTO/RVP) | ~50 | **null**: Firstcry's sheet does not model them |
| Fashion (excl. Babycare) | 24 | Kids Fashion, from the sheet's own formulas (see METHODOLOGY.md) |
| **Babycare** (new, pars 7801-7812) | 12 | Everything except Kids Fashion: transactions, MTU, cancelled, shipped GMV, ASP, payment, city tier, RTO/RVP |

## Analysis and collation layout

- Every Analysis sheet (and History, MoM, YoY and deviation) opens with a **Key metrics** block: gross, shipped
  and net GMV, AOV, ASP, cancellation, returns, MTU, transactions, then category GMV, category % of shipped GMV
  and category ASP. The Output sheet keeps the upload order.
- **Collate** gives one sheet per player: every stored month, then MoM, then YoY, with growth as formulas
  (percentage metrics in points). The comparison tabs follow the same key-first order.
- **The Analysis sheet's current month is formula-driven, as in the master workbooks.** Shipped GMV = Gross GMV x
  (1 - cancellation); returns % = RTO + RVP; net GMV = shipped - returned; transactions = gross x 10^7 / AOV;
  category GMV = category share x shipped GMV, totals are sums, units = GMV x 10^7 / ASP, platform ASP = GMV over
  units; each split's last member = 1 - the others; Fashion price buckets = bucket share x shipped GMV, so they add
  up to it. AOV, category ASP and the shares roll forward in the masters, so they stay inputs. Edit Gross GMV in Excel and shipped, net and every category follow, so the categories keep
  matching shipped GMV. A formula is written only where it reproduces the run's figure, so an unedited
  workbook shows the same numbers. Type a number over any of them to override it.
  On **Edited Analysis**, a cell that moved because an input changed is stored as *Derived* ("recalculated"),
  only the cells you typed over count as edits, and only those feed the learning.
- **Units by category** (total and per category, in '000) close the Analysis: units = category GMV x 10^7 / ASP, as formulas. An Analysis made before they existed can be upgraded on **Edited Analysis > Add the units rows**: the block is added below the last row, so your edits and formulas are untouched.
- Shipped GMV, net GMV, returns % and platform ASP are not in the upload layout, so they appear in the Output's
  "Additional metrics" block below the 86 upload rows.

## Edited Analysis: many files, and the model learns from them

On the **Edited Analysis** tab, upload one edited Analysis workbook per player (several at once) and
you get one Output file per workbook, plus a zip. With **Learn from my edits** ticked (the default):

- the edited month is saved to history as `analyst_edited`, so the next month builds on your figures;
- a category share you moved (directly, or by editing its GMV) becomes a residual in that month's
  learning record; half of it corrects the next month's mix, capped at +/-10% in total;
- the average size of your edits to the headline figures is stored and charted as "edited vs model"
  on the History tab.

Untick it to rebuild the Output only, without touching history.

## Month to month

1. Pick the month. 2. Upload the master workbook, MIS and call. 3. Run, review, download.
4. *Save this month to history*; the next month builds on it. Months must be run in order.

The only prerequisite is that `Revised Babycare` has a column for the month. Validation stops the run and says so.

## Known data points to watch

- **Apr'26 to Sep'26 in `Revised Babycare` are projection formulas** (last month x a factor), not typed actuals. The workbook does not say whether they are actuals, and a history-only run builds on them either way. Confirm before relying on a history-only Oct'26.
- **par_ids 7701-7799 and 7801-7812 are this portal's own.** The real portal par_ids for the babycare-only rows were not supplied; change them in `babycare_template.json`, `base_babycare.json`, `forced_pars.json`, `label_pars.json` and the `_step_babycare_webform` step if the portal assigns others.
- Babycare MTU uses the platform frequency (the sheet has no babycare-only frequency).
- BPC / Home & Furniture rows export as null, never zero.

## Tests

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m pytest tests -q
```

11 tests (about 3 seconds): discovery and start month, per-block par binding, template length and order,
platform identities, the Babycare and Fashion blocks, nulls for BPC/Home, a month with no column refused.
