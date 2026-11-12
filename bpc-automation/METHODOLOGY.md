# Methodology (BPC)

The estimation, weighting, history-balance and calibration logic is the Fashion portal's, unchanged.
The full write-up (evidence weights, the seasonal estimator and its damping, the calendar lever,
reconciliation of level against stated MoM and YoY, double-counting control, calibration) is in
`Downloads\Fashion automation\METHODOLOGY.md`. Its worked numbers are Myntra's and Nykaa Fashion's, not BPC's.
This file records only what is specific to BPC.

## Platform identities

```
Gross GMV          anchors (BPC sheets store gross)
Shipped GMV        = Gross GMV x (1 - cancellation %)
Net GMV            = Shipped GMV x (1 - RTO % - RVP %)
Gross transactions = Gross GMV x 10 / AOV
Category GMV       = category share x shipped GMV        units = GMV x 10 / ASP
```

## Categories

The MIS carries nine lines and the upload sheet three:

| Output category | MIS lines | share / ASP / GMV par_id |
|---|---|---|
| Grooming (personal care) | Body Skin, Face Skin, Hair Care, Hair Care Others, Hair Colour, Hygiene | 1580 / 492 / 249 |
| Makeup & Fragrance | Make up, Fragrance | 1581 / 493 / 250 |
| Others | Others | 1583 / 494 / 4571 |

GMV is the sum of the lines. ASP is the combined GMV over the combined units, where a line's units
are GMV x 10 / ASP. A line with GMV but no ASP is left out of that ASP and the gap is reported
(`MIS_ASP_PARTIAL`). The category mix is the MIS mix; the platform figure sets the level.

## Parameters the BPC sheets do not number

Shipped GMV (243), Returned GMV (244), Net GMV (247), Gross Transactions (4257), Basketsize (519),
Category ASP (468) and the three category GMV rows (249, 250, 4571) carry no par_id in most sheets.
`label_pars.json` binds each by its label, the first row only, and only if no other row holds that
par_id. A test checks each binding: category GMV over shipped GMV must equal the category's share row.

## Choices that differ from Fashion

| | Fashion | BPC |
|---|---|---|
| AOV | ASP x basket size | Own history, or the MIS figure if within 65-135% of history |
| Category tree | Apparel L1/L2/L3, footwear, accessories | Three categories |
| Category-level rows | Separate series | Mirror the platform rows (`mirrors`) |
| Upload sheet | Myntra-WF, reordered by section | 178 rows in the delivered order, additions after |
| Output template | Read from the workbook | Shipped with the portal (`bpc_template.json`) |

## Limits

- No filled BPC MIS existed when this was built. The MIS reader was tested on a Nykaa MIS built
  from the real template with synthetic figures (`tests/fixtures`), so the first real MIS may surface
  label or unit differences. They are reported as findings, not guessed.
- Unit economics, delivery-model split, Reliance and channel rows are in the template but have no
  history in the sheets, so they are exported as null.
- Several reference sheets are loosely kept (placeholder months, `#DIV/0!`, zeros). The engine
  reports these; it does not repair them.
