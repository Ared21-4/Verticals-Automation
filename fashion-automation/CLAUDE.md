# Working in this project

## Model use
- Default is **Sonnet**. Use Opus only for: designing or changing estimation/calibration
  methodology, resolving a genuinely ambiguous requirement, or debugging a numerical
  discrepancy that survives two attempts. Everything else (UI, exports, tests, docs, config,
  wiring a new player) is Sonnet work.
- The product itself calls **no LLM**. Parsing, arithmetic, weighting and export are
  deterministic code. Do not add an LLM call where code will do.

## Keep token use low
- Never dump config JSON (`base_fashion.json`, `myntra.json`) or whole workbooks into context.
  Read a few lines with an offset, or query with a short script.
- Run tests as `python -m pytest tests -q --no-header -p no:warnings -x` and read only the tail.
- Put throwaway scripts in `scratch/`; print counts and a handful of rows, not full tables.
- Prefer `Edit` for small changes over rewriting files.
- Write config files with the file tool or .NET, not `Set-Content -Encoding utf8`: Windows
  PowerShell 5.1 adds a BOM that breaks JSON and TOML.

## Conventions
- Players are listed in `fashion_intel/config/fashion_players.json` (13 fashion players).
- Metrics are addressed by `par_id`, never by row number. Adding a player's depth means a
  `config/players/<key>.json` with only what differs from `base_fashion.json`.
- Every number carries a provenance. Only a MIS value is `reported`. Missing means `null`,
  never zero, and never a projection from a series that ended more than 6 months ago.
- The user uploads a reference each month and it wins. `reference/Fashion Reference.xlsx` is only a
  fallback; saved months live in `data/history/`. Nothing edits the user's original workbook.
- The deliverable format is the Myntra layout: Contents, Myntra-WF, Source, Key.

