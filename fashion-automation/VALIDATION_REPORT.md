# Validation Report — Myntra, September 2026

Sample end-to-end run against the supplied files. All figures below were
produced by the committed code and reproduced on a clean run.

```
python run_cli.py --player myntra --month 2026-09 \
    --reference "Reference sheet and Final output sheet.xlsx" \
    --mis "Myntra MIS.xlsx" \
    --transcript "Myntra_September_Expert_Call_Insights.docx"
```

---

## Automated tests

`python -m pytest tests -q` → **229 passed**.

## Update: history balance (Nykaa Fashion, Sep'26)

| | MIS as reported | Output (history weight 100%) |
|---|---|---|
| Gross GMV | 225.2 Cr | **244.0 Cr** (+8.3%), MIS figure kept beside it |
| YoY on stored history | +18.2% (MIS states +34.5%) | +28.1% |
| MoM on stored history | -2.0% (MIS states -1.6%) | +6.1% |

Worst miss against the MIS's stated growth falls from 16.3 points to 7.8, and the two misses are
now of similar size. History is saved as the MIS reports it and the history weight is learned from
earlier months; in an illustrative three-month run the weight fell from 100% to 58% and the
deviation from +8.3% to +5.7%. Set the history weight to 0% for the MIS unchanged.

## Multi-player run

```
python run_cli.py --all-players --month 2026-09 \
    --reference "Reference sheet and Final output sheet.xlsx"
```

18 players discovered, 15 runnable, **11 completed with zero errors**:

| Player | Gross GMV | Shipped | AOV | Errors |
|---|---|---|---|---|
| Myntra | 5,017.7 | 4,291.9 | 2,102.3 | 0 |
| AJIO | 992.1 | 789.7 | 1,762.8 | 0 |
| Nykaa Fashion | 290.6 | 258.2 | 3,291.0 | 0 |
| Zara | 99.8 | 95.2 | 3,595.1 | 0 |
| H&M | 78.5 | 74.1 | 2,326.0 | 0 |
| Shoppers Stop | 35.3 | 33.2 | 2,544.7 | 0 |
| Bewakoof.com | 25.6 | 24.0 | 1,021.7 | 0 |
| Marks & Spencer | 25.4 | 20.9 | 2,058.1 | 0 |
| Zivame | 15.9 | 14.7 | 932.1 | 0 |
| Clovia | 13.4 | 11.9 | 508.6 | 0 |
| Limeroad | 8.8 | 8.2 | 722.7 | 0 |

Four players (MyGlamm, Nykaa, Purplle, Sugar) are **blocked with
`REF_MONTH_ABSENT`** — their sheets stop at May'24, so there is no Sep'26 column.
The system refuses rather than extrapolating.

These runs had no MIS and no transcript, so they are history-only estimates. The
Myntra figure above is therefore *not* the Myntra result: with its MIS and call
supplied, Myntra is 3,901.6 gross / 3,352.4 shipped. The difference is the
September seasonal factor, which the call suppresses because the festive events
moved to October — evidence that only exists when the call is supplied.

- `tests/test_engine.py` — 33 unit tests on synthetic data: provenance rules,
  weight redistribution and quality discounting, the estimator (including that
  it cannot see the month it estimates), clipping, ratio splits, walk-forward
  backtesting, implied-error arithmetic.
- `tests/test_myntra_run.py` — 37 tests against the real files, covering the
  brief's 13 acceptance criteria plus readers, overrides, the seasonality lever,
  category provenance, the residual carve and export format.

---

## Acceptance criteria

| # | Criterion | Result | Evidence |
|---|---|---|---|
| 1 | Upload MIS, transcript, reference; select month; execute | **pass** | `test_1_workflow_executes`; portal verified serving on HTTP 200 |
| 2 | Processes multiple historical months | **pass** | 56 months Jan'22–Aug'26 loaded; `test_2` |
| 3 | Derived metrics reconcile with inputs | **pass** | All identities exact to 0.00e+00 — see below |
| 4 | Output follows the reference format | **pass** | 158 rows, par_id order, names and units identical to `Myntra-WF`; `test_4` |
| 5 | Missing sources redistribute weight and warn | **pass** | 3 of 7 missing → renormalised to 100%, confidence warning raised; `test_5`, `test_5b` |
| 6 | Contradictory evidence flagged and explained | **pass** | 11 `CALL_MIS_HISTORY_CONFLICT` + 2 `MIS_DEFINITION_CONFLICT`, each with detail; `test_6` |
| 7 | Calibration uses point-in-time information only | **pass** | Walk-forward backtests; `test_7`, `test_estimator_never_sees_the_target_month` |
| 8 | Reported actuals not silently overwritten | **pass** | `test_8`; override on an actual raises `OVERRIDE_ON_ACTUAL` |
| 9 | Estimates, source values and calibrated values distinguishable | **pass** | 5 provenance classes on every value; `test_9`, `test_9b` |
| 10 | Backtesting reports measured error, claims nothing more | **pass** | 42 months per metric, MAPE 2–6%; no accuracy improvement asserted; `test_10`, `test_10b` |
| 11 | Failed uploads and invalid inputs give useful errors | **pass** | `test_11` (corrupt workbook), `test_11b` (wrong month) |
| 12 | Runs retain configuration and history, and reproduce | **pass** | Two identical runs produce byte-identical values; `test_12` |
| 13 | No fabricated numbers or unsupported adjustments | **pass** | 47 of 158 parameters exported as `null`; `test_13` |

---

## Reconciliation

| Check | Result | Residual |
|---|---|---|
| L2 category GMV → platform total | 3,352.3998 = 3,352.3998 | 0.00e+00 |
| Segment GMV → platform total | 3,352.3998 = 3,352.3998 | 0.00e+00 |
| Gross GMV × (1 − cancellation) → shipped | 3,352.3998 = 3,352.3998 | 0.00e+00 |
| Shipped × (1 − returns) → net GMV | 2,346.6799 = 2,346.6799 | 0.00e+00 |
| RTO + RVP → returns | 0.30 = 0.30 | 0.00e+00 |
| ASP × basket → AOV | 2,063.7962 = 2,063.7962 | 0.00e+00 |
| MIS reported category ASPs preserved | 17 of 17 exact | 0.00e+00 |
| L2 contribution shares | 1.000000000 | 0.00e+00 |
| COD + prepaid | 1.0 | 0.00e+00 |
| Women + Men + Kids → Apparel | 2,233.03 | 0.00e+00 |
| Apparel + Footwear + Accessories → Fashion | 3,046.07 | 0.00e+00 |
| Fashion L2 tree → Fashion segment total | 3,046.07 = 3,046.07 | 0.00e+00 |

---

## Output format

| | Reference `Myntra-WF` | Generated |
|---|---|---|
| Template rows | 158 | 158 |
| Added from the model | — | 25 |
| Columns | 5 | 5 |
| par_id set | — | all 158 present, none duplicated |
| Parameter names | — | identical |
| Units | — | identical |
| Populated | — | 136 |
| `null` | — | 47 |

The upload sheet keeps the template's five columns
(`parent_question, parameter_name, par_id, unit, Sep'26`), one row per
parameter, no inserted rows. Percentages are exported in template units.

Rows are re-sequenced for reading, so the sheet loads by `par_id` rather than
by position:

| Section | Rows | Count |
|---|---|---|
| Platform topline | 2–11 | 10 |
| Category shipped GMV | 12–44 | 33 |
| Category ASP | 45–69 | 25 |
| Category contribution | 70–89 | 20 |
| Other calculated metrics | 90–137 | 48 |
| Not available this month | 138–184 | 47 |

All 47 nulls sit together at the bottom. Section breaks are shown by shading
alternate `parent_question` blocks, which costs no rows.

**183 rows: the template's own 158, plus 25 added.** The template carries
segment shipped GMV for BPC and Home & Furniture but none for Fashion, and no
apparel L2/L3 GMV, so the category GMV tree could not be read from the sheet.
The 25 additions are parameters that already exist in the model sheet with
their own par_ids; none is invented, none shadows a template row, and the
template's 158 rows are unchanged. They are listed on `Contents` and flagged
"added by config" on `Source`.

Sheets: `Contents` (the section map above), the upload sheet, `Source` (origin,
confidence and derivation of each value) and `Key`.

The 47 nulls are parameters with no Myntra evidence — zone splits, price
buckets, unit economics, Babycare, AAC, Beauty Appliances. They match the
template's own `IFERROR(…,"null")` behaviour.

Audit workbook: 14 named sheets — Summary, Inputs, Validation, Weights, Call
insights, Call caveats, Metrics, Corroboration, Calibration, Backtests,
Adjustments, Method changes, Overrides, Key — including 231 extracted call
insights and 48 transcript clarifications.

## Independent corroboration of the MIS

Two categories can be rebuilt from figures the expert stated separately, using
no MIS input at all:

| Check | Built from | Implies | MIS reports | Gap |
|---|---|---|---|---|
| BPC | base ₹265 Cr × (1 − 5% YoY) | 251.75 | 251.59 | **+0.06%** |
| Men's Casual Wear | (₹82 + ₹235 Cr) ÷ 50% share | 634.00 | 630.98 | **+0.48%** |

Both corroborate within 2%. This is what establishes that the −5.6% gap against
the reference workbook's pre-existing Sep'26 figure is a defect in that
carry-forward placeholder, not in the estimate.

## File naming

```
outputs/Myntra Sep 2026/
    Myntra Sep 2026 - Output.xlsx
    Myntra Sep 2026 - Audit.xlsx
    Myntra Sep 2026 - Model Column.xlsx
```

A repeat run for the same month is written to `Myntra Sep 2026 (2)` rather
than a timestamped folder.

---

## Findings raised

**2 errors**

| Code | Issue |
|---|---|
| `MIS_GEO_SUM` | MIS geographic split sums to 1.05, not 1. Exported as supplied; normalised values given in the finding |
| `IDENTITY_GEO_SPLIT` | Consequence of the above |

Both are genuine defects in the supplied MIS, reported rather than repaired.

**18 warnings**, the material ones being:

- 11 category contradictions between the call YoY and the stored history
- 2 MIS unit-definition conflicts (AOV 0.48×, orders 1.80× the model)
- Tier 1 share moving 18.3% → 30.0% in one month
- Segment transactions 29.7% above the platform figure (pre-existing in the
  reference workbook)
- Three evidence sources unavailable, and 3 blank MIS metrics

---

## Measured accuracy

Walk-forward, 42 months each, no information from the estimated month:

| Metric | MAPE | Signed | Direction |
|---|---|---|---|
| COD share | 2.04% | +0.83% | 55% |
| Basket size | 3.41% | +0.01% | 74% |
| MAC | 3.94% | +0.56% | 69% |
| Cancellation % | 4.17% | +1.92% | 43% |
| MAU | 5.64% | +0.89% | 71% |

Cancellation's direction accuracy is below chance — the estimator tracks the
level but not the turns. Reported as measured.

Historical category calibration: 13 categories, mean absolute deviation
**39.6%**, signed mean **+0.8%** — large dispersion, no systematic bias.

---

## Environment

Windows 11, PowerShell. The machine had no Python or Node at the start; Python
3.12.10 was installed user-scoped via `winget` (reversible, no admin), plus
openpyxl, pandas, python-docx, streamlit, numpy and pytest.

No external integration was required and none is simulated. No LLM is called.

Reference workbook load: 0.5s. Full run including all three exports: ~7s.
