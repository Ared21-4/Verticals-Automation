# Working in this project

Furniture version of the Fashion and BPC Player Intelligence portals (`Downloads\Fashion automation`,
`Downloads\BPC Automation`). Same engine, furniture configuration. Package is `furniture_intel`.

## Keep token use low
- Never dump `config/base_furniture.json` or whole workbooks into context. Read a few lines with an
  offset, or query with a short script.
- Run tests as `python -m pytest tests -q --no-header -p no:warnings -x` and read only the tail.
- Put throwaway scripts in `scratch/`; print counts and a handful of rows, not full tables.
- Write JSON/TOML with the file tool or .NET, not `Set-Content -Encoding utf8`: Windows PowerShell
  5.1 adds a BOM that breaks them.

## Conventions
- The product calls **no LLM**. Parsing, arithmetic, weighting and export are deterministic code.
- Players are listed in `furniture_intel/config/furniture_players.json` (Pepperfry, Urban Ladder).
  `sheet` is the exact tab name (`"Pepperfry Automated"`, `"Urbanladder Automated"`); `start_month`
  is the first month read.
- Metrics are addressed by `par_id`, never by row number. The two sheets disagree on where some
  par_ids sit, so `config/forced_pars.json` overrides them (section-aware, optionally per sheet) and
  `config/label_pars.json` binds rows that carry none. New furniture-only par_ids: 7101-7105
  category GMV, 7110-7115 items, 7121-7125 items share, 7201-7202 unique-visitor split, 7301 returns
  %, 7401 orders/day, 7501-7502 metro/tier-I, 7601 inventory share.
- The deliverable is `furniture_template.json` in its delivered order (the "Furniture Output" sheet
  layout), then an Additional block. Percentages are written as fractions (`output_pct_scale: 1`).
- Identities follow the sheets' own formulas: see METHODOLOGY.md. Returned GMV is a share of *gross*
  GMV for Pepperfry and of *shipped* for Urban Ladder (`players/<key>.json`, `returns_base`).
- Every number carries a provenance. Only a MIS value is `reported`. Missing means `null`, never
  zero, and never a projection from a series that ended more than 6 months ago.
- The user uploads the master workbook each month and it wins. `reference/Furniture Reference.xlsx`
  is only a fallback (rebuild with `build_slim_reference`); saved months live in `data/history/`.
  Nothing edits the user's original workbook.
- The sheets interleave Urban Ladder and Pepperfry columns before May'20 and carry Covid months
  after, so history starts at Apr'21. Do not lower `start_month` without re-checking the columns.
- Channel Distribution (`export/channel.py`): Pepperfry only. It edits the user's own sheet (insert a
  column in the table and in each block, formulas shifted, formats kept), never rebuilds it. Do not
  re-create the layout in code; change `insert_column`/`_copy_column` instead. The source sheet's
  headers have typos (Total block says "May'26" for Jun'26), so check alignment by position.
