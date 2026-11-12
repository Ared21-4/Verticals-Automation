# Working in this project

Babycare version of the Fashion, BPC and Furniture Player Intelligence portals (`Downloads\Fashion automation`,
`Downloads\BPC Automation`, `Downloads\Furnitutre automation`). Same engine, babycare configuration.
Package is `babycare_intel`.

## Keep token use low
- Never dump `config/base_babycare.json` or whole workbooks into context. Read a few lines with an offset,
  or query with a short script.
- Run tests as `python -m pytest tests -q --no-header -p no:warnings -x` and read only the tail.
- Put throwaway scripts in `scratch/`; print counts and a handful of rows, not full tables. Never name one
  after a stdlib module (`inspect.py` shadowed `inspect` and broke numpy).
- Write JSON/TOML with the file tool or .NET, not `Set-Content -Encoding utf8`: Windows PowerShell 5.1 adds a BOM.

## Conventions
- The product calls **no LLM**. Parsing, arithmetic, weighting and export are deterministic code.
- Players are in `babycare_intel/config/babycare_players.json` (Firstcry, sheet `Revised Babycare`).
- Metrics are addressed by `par_id`, never by row number. The sheet repeats the five category labels in four
  blocks, so `config/forced_pars.json` binds them section-aware and `config/label_pars.json` binds rows with no
  par_id. Babycare-only ids: 7700 transactions, 7701 Others GMV, 7711-7715 shares, 7720-7725 units, 7731-7733
  Kids Fashion AOV / Kids Fashion frequency / platform frequency, 7801-7812 the Babycare webform block.
  **These are placeholders**: the real portal ids for 7801-7812 were not supplied.
- The deliverable is `babycare_template.json` (the Firstcry Webform layout, 86 rows). BPC and Home & Furniture
  rows have no source and export as null. Percentages are fractions (`output_pct_scale: 1`).
- The webform blocks are computed in `_step_babycare_webform` (estimate.py) from the sheet's own formulas
  (rows 59-84); see METHODOLOGY.md.
- Every number carries a provenance. Only a MIS value is `reported`. Missing means `null`, never zero, and never a
  projection from a series that ended more than 6 months ago.
- The user uploads the master workbook each month and it wins. `reference/Babycare Reference.xlsx` is only a
  fallback (values of `Revised Babycare`, built from the master). Nothing edits the user's original workbook.
- Apr'26-Sep'26 in the sheet are projection formulas, not typed actuals.
