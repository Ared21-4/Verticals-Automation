# Player Intelligence portals

Four Streamlit portals that turn a month's inputs into an Output workbook, an Analysis workbook and a webform CSV for each player. The engine is deterministic code (no LLM): parsing, arithmetic, weighting and export.

| Folder | Portal | Package |
|---|---|---|
| `fashion-automation` | Fashion (including Snapdeal and Tata CLiQ) | `fashion_intel` |
| `bpc-automation` | Beauty and personal care | `bpc_intel` |
| `babycare-automation` | Babycare (Firstcry) | `babycare_intel` |
| `furniture-automation` | Home and furniture | `furniture_intel` |

Each portal has the same layout of tools in four groups:

- **Monthly run**: upload, weights, validate and run, review.
- **History & collate**: saved history and collated outputs.
- **Deliverables**: rebuild from an edited Analysis, and fill webform CSVs from Output files.
- **Client QC**: compare a client sheet with our Output or Analysis files.

## Running a portal

```
cd <portal folder>
pip install -r requirements.txt
streamlit run app.py
```

or double-click `Start Portal.bat` on Windows (it frees port 8501 first).

## What is not in this repository

Workbooks (`*.xlsx`), saved history (`data/`), run outputs (`outputs/`) and scratch files are left out on purpose: they hold client and benchmark data. Each portal needs its reference workbook in `reference/` (for example `Fashion Reference.xlsx`), built from the master workbook, before it can estimate a month. See each portal's `README.md` and `METHODOLOGY.md`.

## Conventions

- Metrics are addressed by `par_id`, never by row number.
- Every number carries a provenance; a missing figure is `null`, never zero.
- The master workbook the user uploads each month wins over the built-in reference.
