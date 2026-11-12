# Methodology

How the September 2026 Myntra numbers were produced, what was decided, and what
the system does not know.

---

## 0. Multiple players

Every player sheet in the reference workbook shares one shape: `par_id` in
column A, a header row at row 5, months running rightwards. That is what makes
the system general — players are discovered from the workbook rather than
listed in code.

Metrics are addressed by **par_id**, never by row number. Row layouts differ per
player; par_ids do not. 23 par_ids are present in every player, covering the
topline: 241 Gross GMV, 242 Cancellation, 240 AOV, 238 MTU, 245/246 RTO/RVP,
286/287 payment, 476/477/478 geography, 221/222 fulfilment.

Coverage varies widely — Myntra carries 208 metrics, most players 50–109. Three
rules handle that:

1. **A metric the sheet does not carry is unavailable**, not zero and not
   guessed. It is reported as such.
2. **A derivable metric still computes.** Players whose sheet has no row for
   Shipped GMV (par 243) get a slot off-sheet, so `Shipped = Gross × (1 − cancellation)`
   is still produced and reported. Such a slot holds no history, so nothing can
   be *estimated* into it — only derived.
3. **The anchor follows the evidence.** Myntra's MIS reports shipped GMV, so
   shipped anchors and gross is derived. Every other player stores gross, so
   gross anchors and shipped is derived.

Category depth is per player. Myntra has a MIS taxonomy map and an L2/L3 tree.
A player without one produces platform-level output and says so
(`NO_CATEGORY_TAXONOMY`); where a category tree exists but no MIS, the mix comes
from history and is scaled onto the platform total, with every category marked
`estimated`.

Configuration is three layers: `base_fashion.json` → `players/<key>.json` →
the player's own sheet. A player with no file of its own still runs.

---

## 1. The reference model

`Myntra_AJ` in the reference workbook is the live model: 458 metric rows keyed
by `par_id`, monthly columns Jan'22 → Sep'26. It is a deterministic dependency
graph with a small number of free inputs. The engine reproduces that graph.

Platform identities, taken from the workbook's own formulas:

```
Gross GMV        = Shipped GMV / (1 − cancellation %)
Net GMV          = Shipped GMV × (1 − RTO % − RVP %)
ASP              = Shipped GMV × 10 / shipped units
Gross transactions = Gross GMV × 10 / AOV
Basket size      = AOV / ASP
MTU              = MAU × MAC
Frequency        = gross transactions / MTU
```

Category identities, per L2 and per segment:

```
units            = GMV × 10 / ASP
segment GMV      = Σ L2 GMV           segment units = Σ L2 units
segment ASP      = segment GMV × 10 / segment units
contribution %   = L2 GMV / platform shipped GMV
```

`Myntra-WF` is the output format: a flat 158-row table of
`parent_question, parameter_name, par_id, unit, value`. 114 of those par_ids
exist in the model. The other 44 — zone splits, price buckets, unit economics,
Babycare — have no Myntra data and are exported as `null`, matching the
template's own `IFERROR(…,"null")` behaviour.

---

## 2. Decisions taken

Four ambiguities materially changed the output. All four were resolved with the
client before implementation.

| # | Decision | Effect |
|---|---|---|
| 1 | **The MIS 17 categories are the whole platform.** Watches and Fashion Accessories have no MIS line, so they are carved out of the MIS total at their Aug'26 GMV share (2.11%, 70.7 Cr) and the mapped categories scale by 0.9789. Units are carved at their own, much lower, Aug'26 unit share (1.15%), because these two carry roughly double the book's average ASP | Platform total stays exactly 3,352.4 Cr; the MIS reported category ASPs are preserved exactly |
| 2 | **Keep model definitions for AOV, orders and MTU.** The MIS values conflict 2–5× and are logged, not applied | Every metric derived from them stays coherent |
| 3 | **Lock history; measure and report its error.** No stored value is rewritten | Produces the calibration table in §6 |
| 4 | **The MIS is a reported actual and a hard anchor.** Weights cannot move it | Shipped GMV and the category mix are not re-weighted |

---

## 3. Evidence weighting

Configured weights state standing policy. Effective weights are what a run
actually used, after removing absent sources and discounting weak ones. Both
are exported.

For the September 2026 run, three of seven sources were not supplied:

| Source | Configured | Available | Quality | Effective |
|---|---|---|---|---|
| Player MIS | 35% | yes | ×1.0 | **53.85%** |
| Expert call | 35% | yes | ×0.6 (garbled passages) | **32.31%** |
| Calendar & seasonality | 5% | yes | ×1.0 | **7.69%** |
| Platform sale events | 4% | yes | ×1.0 | **6.15%** |
| Logistics | 10% | **no** | — | 0% |
| Traffic | 8% | **no** | — | 0% |
| Social | 3% | **no** | — | 0% |

Remaining weights are renormalised proportionally, so relative importance
between surviving sources is preserved. 79% of configured weight was present,
which sets run confidence to *high* but with explicit warnings.

The expert call was discounted to ×0.6 because the transcript itself flags 47
garbled or internally inconsistent passages. Availability and trustworthiness
are tracked separately.

**Weights are not GMV coefficients.** They are consulted only where a parameter
has competing evidence. A reported actual under decision 4 is never re-weighted.

---

## 4. How each parameter was produced

| Provenance | Count | Meaning |
|---|---|---|
| `reported` | 60 | Stated in the MIS. An actual |
| `derived` | 135 | Arithmetic on other values, using the workbook's own formula |
| `estimated` | 25 | Model estimator, from history ending before this month |
| `assumed` | 7 | Carried forward; no evidence this month |
| `unavailable` | 2 | No evidence. Exported as `null`, never zero |

**From the MIS (anchors):** shipped GMV, the 17 category GMVs and ASPs, the
returns rate, the geographic split.

**Estimated:** cancellation %, basket size, MAU, MAC, COD share, segment users
and baskets, L2 residual shares.

**Estimator.** One estimator is used for every level, so it can be backtested
and explained:

```
estimate = (trailing 3-month mean) × (seasonal factor for this calendar month)
seasonal factor = mean over prior years of (that month ÷ the 3 months before it)
clipped to the trailing 12-month range ±15%
```

History passed to the estimator always ends strictly before the target month.

### The calendar lever, and why it was suppressed

Seasonal factors are learned from the same calendar month in prior years. Prior
Septembers were festive; September 2026 was not, because the festive events
moved 15–20 days later into October. The expert states this directly
("purely because of the event date difference"; Dussehra and Diwali both fall in
October this year).

Applying a September factor learned from festive Septembers would import an
event that did not happen. The engine detects the shift from the transcript and
holds estimates at the trailing level. The effect is material:

| | factor applied | suppressed | Aug'26 actual |
|---|---|---|---|
| Basket size | 2.78 | **2.59** | 2.46 |
| AOV | ₹2,213 | **₹2,064** | ₹1,930 |
| Cancellation | 14.46% | **14.08%** | 14.00% |

This is overridable per run (`auto` / `bau` / `festive`).

### How headline GMV is anchored, and why history alone can over-quote

Evidence is used in this order, and each step is labelled in the output:

1. **MIS figure** → `reported`.
2. **A GMV figure stated on the call** (and no MIS) → `call_stated`, medium confidence.
3. **History alone** → `estimated`, low confidence, with a range and a warning.

Step 3 is the least reliable number the system produces. For Nykaa Fashion, Sep'26 it gave
**290.6 Cr of gross GMV, 29% above the 225 Cr the call and MIS agree on**:

| | |
|---|---|
| Trailing level, Jun–Aug'26 as stored | 238.8 (the true run-rate was about 225, so already +6%) |
| September uplift learned from Sep'23/24/25 | ×1.217 (each was 13–28% above its prior three months) |
| History-only estimate | 238.8 × 1.217 = **290.6** |

The uplift was real in earlier years but did not occur in 2026: the call says August and
September were both ordinary months. History alone cannot know that. Stored Sep'25 (190.5) is
also about 14% above the ~167 Cr the call gives, so the pattern was learned from an inflated month.

A walk-forward test across all 11 original players (Snapdeal and Tata CLiQ were added later) (each month estimated only from earlier months) shows
this is a tail event, not a steady bias:

| Seasonal strength | 0 (none) | 0.25 | 0.5 | 0.75 | 1.0 (full) |
|---|---|---|---|---|---|
| Average error | 9.0% | 7.9% | 7.3% | **7.2%** | 7.8% |
| Worst single month | 25.1% | 23.6% | **22.7%** | 24.5% | 27.8% |
| September months only | 10.8% | 9.3% | 8.3% | 7.6% | **7.0%** |

Seasonality helps on average, so it is kept, but damping it lowers both average and worst-case
error. Nykaa Fashion's own typical error is about 10%, so a 29% miss is well outside it.

Safeguards now in place:

- **Better evidence first.** A call that states the GMV, or says the month was BAU, overrides the
  history estimate. Nykaa: nothing supplied 277.6 (+23%), call only 225.0, MIS 225.2.
- **Damped uplift.** For headline GMV the seasonal swing is scaled by a strength chosen from the
  player's own past errors, using only months before the target. Where two strengths are within
  3% of each other the gentler one is taken.
- **Range and warning.** A history-only GMV shows the range from no uplift to full uplift and
  states how far this method has missed for that player (`GMV_HISTORY_ONLY`).
- **History check against the call.** A year-ago figure on the call is compared with stored
  history (`HISTORY_BASE_CONFLICT`). Stored values are never edited.
- **Save to history.** Saving a month with a MIS replaces the placeholder, so next year's seasonal
  factor is learned from the real figure.

What this does not do: with no MIS and no call, a month that breaks its usual pattern can still
miss by 20% or more. The system says so rather than hiding it.

### Reconciling the level with the growth the MIS states

A MIS gives a level and also its MoM and YoY, measured against the *stored* history. The three
can disagree, and the question is which input is wrong. Nykaa Fashion, Sep'26:

| Signal | Implied level | vs reported 225.2 |
|---|---|---|
| MIS level | 225.2 | reported |
| Stored Aug'26 (229.8) x MIS MoM (-1.6%) | 226.2 | +0.4% |
| Call's year-ago base (~167) x MIS YoY (+34.5%) | 224.6 | -0.3% |
| Stored Sep'25 (190.5) x MIS YoY (+34.5%) | 256.2 | **+13.8%** |

The reported level fits the MoM but not the YoY. Which input is wrong is a judgement, and the
call's own year-ago base (~167 Cr) points at the stored Sep'25. But the stored reference is the
history the analyst works from, and a level that sits 16 points off the YoY it was reported with
is hard to use. So the output is not simply the MIS figure.

### The history balance

The stored history is used to keep MoM and YoY from being far off each other:

1. **Find the history-consistent level.** The level whose MoM and YoY, measured on the stored
   history, are jointly closest to the ones the MIS states (least squares, closed form). For
   Nykaa Fashion that is **244.0 Cr**: YoY +28.1% and MoM +6.2%, against the MIS's +34.5% and
   -1.6%. Neither is matched exactly, and neither is far off. Forcing YoY alone to +34.5% would
   give 256 Cr and a +11% MoM.
2. **Move the output toward it.** The output moves the **history weight** of the way (default
   100%, adjustable per player in the portal, 0% = the MIS unchanged), by no more than the cap
   (15% of the MIS figure). Gross GMV becomes `calibrated`; the MIS figure is kept on the value
   and shown beside the output.
3. **Everything downstream follows**: shipped, net, AOV-derived transactions and the category
   tree are built from the balanced gross GMV, so they stay consistent with it.
4. **Close the gap over months.** Two things narrow the distance between output and MIS:
   - History is **saved as the MIS reports it**, not as the balanced output. Next month's MoM
     base is therefore the player's own figure, and the stored bases move toward the MIS.
   - The history weight is **learned**: if history sat far from the MIS in earlier saved months
     it is weighted less (`weight x (1 - gap / 20%)`, floor 25%), and more if it sat close.
   The deviation each month, and the history weight used, are recorded on the Balance sheet.

Rules:

- Within 5%, the growth is "consistent" and nothing moves.
- With only one growth rate stated, the level moves toward the level that rate implies.
- The output is never more than the cap from the MIS figure, and the MIS figure is always shown.
- Stored history is never edited. Saved months are the MIS-based figures.

What it costs: the output now differs from the MIS (8.3% for Nykaa, Sep'26), and from the ~225 Cr the
expert confirmed. That is the trade made to keep growth consistent with the history you supply. If
the stored Sep'25 is the real problem, correcting it in the reference removes the gap at its source.

### Double-counting control

Every material adjustment registers the business driver it represents. The
festive-timing driver is claimed once, by the calendar lever. When the call's
category YoY later attributes the same weakness to the same cause, the engine
records that the driver is already claimed and applies no second adjustment.

---

## 5. Methodology change against the reference workbook

One change, documented and exported.

**AOV (par 240).** The workbook carries AOV forward with a hardcoded multiplier
(`=BG15*0.97`) and derives basket size from it. That makes the month's AOV an
unevidenced guess. Instead:

```
ASP    = shipped GMV × 10 / units,  units summed from MIS category GMV ÷ ASP
basket = estimated from its own history
AOV    = ASP × basket size
```

Basket size is the more stable series, and this anchors AOV to reported
category data. The identity `basket = AOV / ASP` still holds, so nothing
downstream changes shape. September 2026 AOV is ₹2,064 against ₹1,872 under the
workbook's carry-forward.

No metric definition was changed.

### Provenance follows how a category reached the taxonomy

A category is only a reported actual where it maps one-to-one onto a MIS line.

| Mapping | Example | Provenance |
|---|---|---|
| One-to-one | Women's Ethnic → Women's ethnicwear | `reported` |
| Several MIS lines into one | Men's LTA + Women's LTA → LTA | `reported` (GMV), `derived` (ASP) |
| One MIS line split several ways | Personal Care → Grooming + Makeup | `derived` |
| No MIS line at all | Watches, Fashion Accessories | `estimated` |

Splits are allocated on the prior month's mix, so they are arithmetic on a
reported total rather than reported figures themselves.

---

## 5b. Independent corroboration of the MIS

Some expert figures are stated in a form that owes nothing to the MIS — a base
and a growth rate, or a price-point build-up and the share it represents.
Rebuilding a category from those and comparing it with the reported value tests
the MIS instead of repeating it.

| Check | Built from | Implies | MIS reports | Gap |
|---|---|---|---|---|
| BPC | base ₹265 Cr × (1 − 5% YoY) | 251.75 | 251.59 | **+0.06%** |
| Men's Casual Wear | (₹82 + ₹235 Cr) ÷ 50% share | 634.00 | 630.98 | **+0.48%** |

Two independent reconstructions land within half a percent of the reported
figure. That is the basis for treating the **−5.6% gap against the reference
workbook's pre-existing Sep'26 value as a defect in that placeholder**, which
was an unevidenced carry-forward, rather than error in this estimate. Moving the
estimate toward 3,552 Cr would move it away from two corroborated sources.

Checks are configured per player under `corroboration`, so adding one is a
config change. They never alter a number; they produce a verdict.

---

## 6. Calibration

### What was measured

Combining each reported September 2026 category actual with the expert's stated
YoY back-implies what September 2025 must have been. Comparing that with the
stored September 2025 value measures the error in the stored history.

The strongest corroboration is BPC. The expert gave a September 2025 base of
≈₹265 Cr and −5% YoY, implying ₹251.75 Cr. The MIS reports **₹251.59 Cr** — a
0.06% match from two independent sources. Redseer's stored Sep'25 BPC is
₹214.4 Cr, understated by 23.6%.

Across 13 categories:

| | |
|---|---|
| Mean absolute deviation | **39.6%** |
| Signed mean deviation | **+0.8%** |
| Conclusion | **No material systematic bias, very large dispersion** |

Largest deviations:

| Category | stored Sep'25 | implied Sep'25 | deviation |
|---|---|---|---|
| Women's Footwear | 249.3 | 138.0 | +80.7% overstated |
| Kids Wear | 283.8 | 164.8 | +72.2% overstated |
| Women's Western Wear | 835.1 | 485.5 | +72.0% overstated |
| Women's Sports Apparel | 12.8 | 43.2 | −70.5% understated |
| Jewellery | 63.8 | 159.1 | −59.9% understated |
| Sports Footwear | 163.8 | 351.2 | −53.4% understated |

The practical reading: the historical **platform total** is roughly right, but
the **category mix within it** is unreliable. Errors are large and offsetting,
not directional. A flat percentage correction would achieve nothing — which is
why none is applied.

Stored values were not changed. Bias terms are retained for future months, are
capped at ±25%, and are never applied to a reported actual.

### Measured forecast accuracy

Separately, each estimator is walked forward through history: at month *t* it
sees only data before *t*. These are genuine out-of-sample errors.

| Metric | Months | MAPE | Signed | Direction |
|---|---|---|---|---|
| Cancellation % | 42 | 4.17% | +1.92% | 43% |
| Basket size | 42 | 3.41% | +0.01% | 74% |
| MAU | 42 | 5.64% | +0.89% | 71% |
| MAC | 42 | 3.94% | +0.56% | 69% |
| COD share | 42 | 2.04% | +0.83% | 55% |

Cancellation's direction accuracy of 43% is below a coin toss: the estimator
tracks the level well but does not predict turns. That is reported, not hidden.

**No improvement in accuracy is claimed.** Confirmed actuals for this month's
estimated parameters do not exist yet. Accuracy can only be measured when they
arrive.

---

## 7. September 2026 result

| Metric | Value | Provenance |
|---|---|---|
| Shipped GMV | **3,352.4 Cr** | reported (MIS) |
| Gross GMV | **3,901.6 Cr** | derived |
| Net GMV | **2,346.7 Cr** | derived |
| Cancellation | 14.08% | estimated |
| Returns | 30.00% | reported (MIS) |
| ASP | ₹796.03 | derived from MIS |
| AOV | ₹2,063.80 | derived |
| Basket size | 2.59 | estimated |
| Gross transactions | 18.91 Mn | derived |
| MTU | 16.53 Mn | estimated |

Against the workbook's pre-existing placeholder of 3,552 Cr shipped /
4,126.9 Cr gross, this run lands **5.6% lower on shipped GMV**, because the
placeholder was a carry-forward and this is the reported actual.

---

## 8. Open issues

Carried on every run rather than resolved silently.

1. **The MIS geographic split sums to 1.05** (Metro 35% + Tier 1 30% + Tier 2+
   40%). The MIS is internally inconsistent. Shares are exported as supplied;
   normalising would change a reported figure, so it is left as an explicit
   analyst decision. Normalised values are given in the finding.

2. **Tier 1 share moves 18.3% → 30.0% in one month.** Too large to be a business
   movement; reads as a definition change. Applied because the MIS is the
   anchor, flagged prominently.

3. **MIS AOV ₹986 / orders 34M / MTU 80M.** Internally consistent
   (34M × 986 = 3,352 Cr) but 2–5× from the model's definitions. Reads as
   shipment-level rather than order-level, and a reached-base rather than
   transacting users. Logged, not applied. **Worth confirming with Myntra.**

4. **Segment transactions do not sum to the platform figure** (24.52 Mn vs
   18.91 Mn, +29.7%). This is a pre-existing inconsistency in the reference
   workbook — the two are derived on different AOV bases, and the same gap
   appears in its own historical columns. Reported, not forced.

5. **Sub-category splits use the prior-month mix.** MIS "Sports Footwear"
   (combined), "Personal Care" (→ Grooming/Makeup) and "Home Furnishing" (→ four
   H&F lines) are split on Aug'26 proportions. Given §6, those proportions are
   not reliable. The totals are reported actuals; the splits within them are not.

6. **Watches and Fashion Accessories are model estimates inside a reported
   total** — 70.7 Cr, no MIS evidence.

7. **No traffic, logistics or social data.** MAU, MAC, captive/3PL split and all
   session metrics rest on their own history. 19% of configured evidence weight
   was unavailable.

8. **Cancellation could not be back-solved.** The MIS leaves Gross GMV at SP
   blank, so cancellation is estimated rather than derived. Filling that one MIS
   cell would turn Gross GMV from an estimate into a reported-actual derivation.

9. **One month of MIS evidence.** The calibration in §6 rests on a single
   month's overlap. It is a measurement, not yet a trend.

---

## 9. Where an LLM is and is not used

No LLM is used in this pipeline.

The transcript is a structured document — headings, typed tables, an Appendix A
quick-reference table and an Appendix B clarifications log. A deterministic
parser extracts 230 insights, 13 category YoYs and 47 clarifications with exact
provenance, which an LLM could not improve on and could silently corrupt.

For a genuinely unstructured transcript, the intended path is an LLM confined to
extraction and classification, emitting a schema that is validated before any
number reaches a calculation. Arithmetic stays in code either way.

The system does not invent figures, sources, relationships or confidence scores.
Where evidence is insufficient it returns an explicit uncertainty flag and a
`null`.
