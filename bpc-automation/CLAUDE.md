# Working in this project

BPC version of the Fashion Player Intelligence portal (`Downloads\Fashion automation`). Same engine,
BPC configuration. Package is `bpc_intel`.

## Keep token use low
- Never dump `config/base_bpc.json` or whole workbooks into context. Read a few lines with an
  offset, or query with a short script.
- Run tests as `python -m pytest tests -q --no-header -p no:warnings -x` and read only the tail.
- Put throwaway scripts in `scratch/`; print counts and a handful of rows, not full tables.
- Write JSON/TOML with the file tool or .NET, not `Set-Content -Encoding utf8`: Windows PowerShell
  5.1 adds a BOM that breaks them.

## Conventions
- The product calls **no LLM**. Parsing, arithmetic, weighting and export are deterministic code.
- Players are listed in `bpc_intel/config/bpc_players.json` (11 BPC players). The `sheet` value is
  the exact tab name, trailing spaces included (`"Purplle  "`).
- Metrics are addressed by `par_id`, never by row number. BPC sheets leave many rows without one
  (Shipped GMV, Returned GMV, Basketsize, the category GMV rows); `config/label_pars.json` binds
  those by label, first row only, and only if no row already carries that par_id.
- The deliverable is the 178-row upload sheet in `config/bpc_template.json`, in its delivered order,
  with the model's additions (shipped/net GMV, category GMV ...) in a separate block after it.
- The MIS has nine category lines; the output has three (Grooming = personal care, Makeup &
  Fragrance, Others). `mis_generic.categories` maps lines to par_id sets and `_roll_up` merges them.
- `mirrors` in the base config repeats platform rows as the "Beauty & Personal Care" category rows.
- Every number carries a provenance. Only a MIS value is `reported`. Missing means `null`, never
  zero, and never a projection from a series that ended more than 6 months ago.
- The user uploads a reference each month and it wins. `reference/BPC Reference.xlsx` is only a
  fallback; saved months live in `data/history/`. Nothing edits the user's original workbook.
