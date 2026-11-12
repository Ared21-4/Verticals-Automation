# Methodology (Furniture)

The estimation, weighting, history-balance and calibration logic is the Fashion portal's, unchanged
(see `Downloads\Fashion automation\METHODOLOGY.md`; its worked numbers are Myntra's and Nykaa
Fashion's). This file records only what is specific to furniture.

## Platform identities (taken from the player sheets' own formulas)

```
Gross GMV           anchors (the sheets store gross)
Shipped GMV         = Gross GMV x (1 - cancellation %)
Returned GMV        = Gross GMV x returns %      Pepperfry
                    = Shipped GMV x returns %    Urban Ladder      (returns_base)
Net GMV             = Shipped GMV - Returned GMV
Gross transactions  = Gross GMV x 10^7 / AOV                      (a count, not Mn)
MTU (Mn)            = Gross transactions / 10^6
Orders shipped/day  = Shipped GMV x 10^7 / AOV / days in month
Category GMV        = category share x Shipped GMV                 (five categories)
Category items      = Category GMV x 10^7 / Category ASP
Items shipped       = sum of the categories
Platform ASP        = Shipped GMV x 10^7 / Items shipped           (a MIS-reported ASP is kept)
Metro (output row)  = Metro % + Tier 1 %     Others = Tier 2+ (the sheets' 2-way "Metro" row)
```

Returns % is the sheet's own "Returns(RTO+RVP)" row, estimated directly (`returns_direct`). Urban
Ladder's RTO and RVP rows are on a different base and do not sum to it, so no RTO + RVP = returns
check is run for furniture.

## Categories

Home furnishings, Furniture, Home Décor, Home improvement tools, Household. Share par_ids
1426-1430, ASP 498/499/500/502/503, GMV 7101-7105. Urban Ladder carries only the first three (tools
and household are blank in recent months, so they export as null).

## Channel distribution (Pepperfry)

```
Channel GMV by category = online (or studio) share x that channel's category mix x Shipped GMV
Contribution            = sum over both channels / Shipped GMV
Check                   = platform category share (Output) - Contribution
```

The run's category shares (par 1426-1430) are the target. With online share o fixed at the last
three months' mean, each category's gap `r = share - (o x on0 + (1-o) x off0)` is closed by moving
`on` and `off` in proportion to their own squared size (so each moves about the same relative
amount); `on` is then rescaled to total 1 and `off` follows from the identity. A 0.05% floor keeps
mixes positive; any residual it causes shows in Check. The sheet's last typed month (Sep'26) does not
itself reconcile (up to 4.7 points on Furniture), so the first reconciled month moves the mixes by
that much.

## Choices that differ from Fashion and BPC

| | Fashion / BPC | Furniture |
|---|---|---|
| Players | 11 each | 2 (Pepperfry, Urban Ladder) |
| History read from | first month in the sheet | Apr'21 (`start_month`) |
| Gross transactions | Mn | count (#); MTU = transactions / 10^6 |
| Upload percentages | 12.5 | 0.125, formatted as % |
| Wrong-player MIS guard | stop outside 0.2x-5x of last month | stop outside 0.6x-1.6x (`mis_level_bounds`) |
| Template | 178 rows (BPC) | 67 rows, the Furniture Output layout |

The tighter MIS bound exists because the two furniture players differ by only about 2x, so the
fashion/BPC bound would let one player's MIS through for the other.

## Limits

- No furniture MIS or expert-call write-up existed when this was built. The MIS reader is the
  generic one (sections Overall, Category GMV, Category ASP, Payment Split, Geographic Split) and was
  tested on a synthetic Pepperfry MIS (`tests/fixtures`); the first real MIS may surface label or unit
  differences. They are reported as findings, not guessed. Category lines are matched by name in
  `mis_generic.categories` (furniture, home furnishings, home decor, home improvement tools, household).
- Gross GMV is a formula (for example last month x a factor) rather than a typed figure in every
  month since May'25 for Pepperfry and since Dec'25 for Urban Ladder. Whether those months are
  actuals or the sheet owner's projections is not recorded in the workbook, and a history-only run
  builds on them either way. Check before relying on them.
- Unique visitors, Fulfilled+ share and Tier-I have no usable history in Pepperfry and export as null.
  Urban Ladder's unique-visitor unit is "Cr" in the sheet but the magnitudes do not match its sessions;
  it is carried as the sheet has it.
- The sheets' helper rows 82-90 are not part of the model; only Pepperfry's RTO%/RVP% rows (89/90)
  are read, because its own RTO/RVP rows (17/18) are empty.
