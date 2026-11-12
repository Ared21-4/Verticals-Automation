# Methodology (Babycare)

The estimation, weighting, history-balance and calibration logic is the Fashion portal's, unchanged
(see `Downloads\Fashion automation\METHODOLOGY.md`). This file records only what is specific to babycare.

## Platform identities (taken from the Revised Babycare sheet's own formulas)

```
Gross GMV            anchors (the sheet stores gross)
Shipped GMV          = Gross GMV x (1 - cancellation %)
Returns %            = RTO + RVP                     (row 12)
Net GMV              = Shipped GMV x (1 - returns %)
Monthly transactions = Gross GMV x 10 / AOV           (Mn)
MTU                  estimated directly (the sheet types it; it is not derived from transactions)
Category GMV         = category share x Shipped GMV   (Kids Fashion, Diaper & Nappies, Supplies, Gear, Others)
Category units ('000)= Category GMV x 10^7 / ASP / 10^3
Platform ASP         = Shipped GMV x 10^4 / total units
```

## Webform blocks (replicating rows 59-84 of the sheet)

```
Kids Fashion gross GMV = Kids Fashion share x Gross GMV
Kids Fashion AOV       = last month's x (Kids Fashion ASP / last month's ASP)
Fashion gross txns     = Kids Fashion gross GMV x 10 / Kids Fashion AOV                 (par 4254)
Platform frequency     = monthly transactions / MTU
Kids Fashion frequency = last month's x (platform frequency / last month's)
Fashion MTU            = Fashion gross txns / Kids Fashion frequency                    (par 3616)

Babycare AOV           = (AOV - share_KF x KF AOV) / (1 - share_KF)
Babycare gross txns    = (Gross GMV - Kids Fashion gross GMV) x 10 / Babycare AOV       (par 7801)
Babycare MTU           = Babycare gross txns / platform frequency                       (par 7802)
Babycare shipped GMV   = Diaper + Supplies + Gear + Others                              (par 7804)
Babycare ASP           = Babycare shipped GMV x 10^4 / Babycare units                   (par 7805)
Cancelled, payment, city tier, RTO, RVP = the platform figures, unchanged               (7803, 7806-7812)
```

"Babycare" means everything except Kids Fashion, as the sheet's own "Babycare Gross Transactions" does.

## Choices that differ from Fashion, BPC and Furniture

| | Fashion / BPC | Furniture | Babycare |
|---|---|---|---|
| Players | 11 | 2 | 1 (Firstcry) |
| History read from | first month | Apr'21 | Jan'21 |
| Gross transactions | Mn | count (#) | Mn |
| Units | - | items (#) | '000 |
| Returns | RTO + RVP | direct | RTO + RVP (returned GMV on shipped) |
| Wrong-player MIS guard | 0.2x-5x | 0.6x-1.6x | 0.6x-1.6x |
| Template | 178 rows | 67 rows | 86 rows (Firstcry Webform) |
| Channel Distribution | - | Pepperfry | none |

## Limits

- No Firstcry MIS or expert-call write-up existed when this was built. The MIS reader is the generic one and
  has not been run against a real Firstcry MIS; label or unit differences will be reported as findings, not guessed.
- Apr'26-Sep'26 in `Revised Babycare` are projection formulas, not typed actuals.
- Traffic, user-base, app and logistics metrics are not in the Revised Babycare sheet; they are reported unavailable.
- BPC and Home & Furniture webform rows are not modelled for Firstcry and export as null.
- Babycare MTU uses the platform frequency; Kids Fashion frequency and AOV chain off last month's value, so they
  need the previous month's column (or a saved month) to be populated.
