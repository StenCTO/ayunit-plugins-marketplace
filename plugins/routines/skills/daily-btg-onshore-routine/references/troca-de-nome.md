# Recipe: FII "TROCA DE NOME" — non-cash asset swap

BTG occasionally consolidates or splits FII ticker classes (share-class
reorganizations). The event delivers as a `BUY` on the receiving ticker
paired with `SELL` rows on the delivered tickers, all with
`launchType = 'TROCA DE NOME'` in `RawTransaction`. The loader promotes
them to `VALIDATED` with peg prices — typically R$1,000/unit on the receive
side and R$0.01/unit on the delivery sides — which the pipeline reads as a
real cash-side leg and posts a **phantom cash debit** for the receive-side
value.

The event is a **pure custody swap** with no economic cash movement. The
correct disposition is to reclassify the `TransactionType` **and** restate
the swap prices at fair market NAVs (not the peg ratios BTG uses to encode
the quantity ratio):

- Receive-side `BUY` → **`ASSET RECEIPT`**
- Deliver-side `SELL` → **`ASSET DELIVERY`**

Per [`ayunit://docs/transaction/types`](ayunit://docs/transaction/types),
`ASSET RECEIPT` has the **same sign convention as `BUY`** (Qty+, Price+,
Value−) and `ASSET DELIVERY` has the **same sign convention as `SELL`**
(Qty−, Price+, Value+). The pipeline consumes both types via §6.9 of
[`ayunit://docs/portfolio-creator/pipeline`](ayunit://docs/portfolio-creator/pipeline):
they do **not** generate the Step-2.5 cash side (no BRL debit/credit), but
they **do** contribute to the asset's `ValueTransaction`, which drives
`PnlExGrossUp = ValueClose − ValueOpen + ValueTransaction`.

**This is why prices/values matter.** If you leave `Value = 0` on an
`ASSET RECEIPT` of a class that has non-zero `PriceClose`, the pipeline
computes a spurious `PnlExGrossUp = ValueClose − 0 + 0 = ValueClose` — a
fake day-1 gain. Symmetric fake losses appear on the `ASSET DELIVERY`
side. The identity `Value = ±|Qty| × Price × ContractSize` must hold so
`ValueTransaction` correctly offsets the market-value change on the swap
day, giving P&L = 0 per asset (which is what a fair-value swap
economically is).

## Detection

Given a set of trades on a shock date (from [`cash-gap-investigation.md`](cash-gap-investigation.md)
Step B), classify each as TROCA DE NOME if **all** of:

- `RawTransaction.launchType = 'TROCA DE NOME'` (case-sensitive, exact), **and**
- `TransactionType` is `BUY` or `SELL` (loader's misclassification), **and**
- `Status IN ('VALIDATED', 'UPDATED')` (already promoted; PENDING rows follow the same pattern but the fix cycle is identical), **and**
- The event participates in a **swap cluster**: on the same `(Account, Date)`
  there is at least one row of the opposite direction with the same
  `launchType`. Verify:

  ```sql
  SELECT TransactionType, COUNT(*) AS n,
         SUM(ABS(Quantity)) AS abs_qty_sum
  FROM Portfolio.v_AccountTransaction
  WHERE ClientAccount = '<account>' AND Custody = '<custody>'
    AND Date = '<shock_date>'
    AND (RawTransaction LIKE '%"launchType": "TROCA DE NOME"%'
      OR CAST(Obs AS varchar(200)) LIKE '%TROCA DE NOME%')
  GROUP BY TransactionType;
  ```

  A valid TROCA cluster has at least one BUY and at least one SELL. The
  `abs_qty_sum` on each side should approximately balance (typically 1:1
  ratio), but do **not** require exact equality — some real reorganizations
  redistribute at other ratios.

If the cluster is BUY-only or SELL-only, do **not** apply this recipe — that
signals a real trade with a mistagged `launchType`, or a partial data feed.
Escalate.

## Prerequisite — resolve fair-market NAV for each participant

For every ticker in the cluster (receive-side and deliver-side), resolve
the fair-market NAV **as of the trade date** (fall back to the last
available price if the delivered ticker is gone from custody after the
swap, since custody positions for the delivered tickers will be zero on
the trade date itself). Best sources, in order:

1. `Portfolio.v_AccountPosition.PriceClose` on the day before the trade
   (`Date − 1`) for the same asset — this is the pipeline's own resolved
   NAV, so consistent by construction. Take it from **any** account
   holding the ticker, not just the one under repair.
2. `Portfolio.v_CustodyPosition.Price` on the day before the trade for the
   same asset (BTG's own valuation).
3. `mcp__ayunit__get_price_history` for the ticker over the trade window.
4. `Global.v_Asset` (limited — only bond-like assets carry a fixed
   `Price`).

If **any** ticker in the cluster can't be resolved, do **not** apply the
recipe — surface as human-action. Do not use the raw peg-ratio prices
(`R$1000` / `R$0.01`) as substitutes; those are quantity encoders, not
market values.

## Fix recipe

For **each** row in the cluster, apply a single SELECT-first-merge `@CMD='U'`:

1. Fresh SELECT of every populated column of the row.
2. Drop `AccountCurrency` and `AccountFx`.
3. Preserve `RawTransaction` verbatim (it retains the original peg-price
   payload; the analyst may need it later for reconciliation with BTG).
4. Absolute values for `Quantity` / `Price` / `PriceExFee` / `Value` /
   `ValueGross` — the proc applies the sign per `TransactionType`.
5. **Overlay:**
   - `TransactionType`: `BUY → 'ASSET RECEIPT'`, `SELL → 'ASSET DELIVERY'`.
   - `Price` = `PriceExFee` = **fair-market NAV** (from the prerequisite
     step, absolute). No FII carries brokerage/loading on a swap, so
     `Price = PriceExFee`.
   - `Value` = `ValueGross` = `|Quantity| × PriceExFee × ContractSize`
     (absolute; the proc applies the negative sign for `ASSET RECEIPT`
     and the positive sign for `ASSET DELIVERY`). `ContractSize` for
     FIIs is `1`; verify via `identify_asset` if the asset class is
     unfamiliar.
   - The identity `Value = |Qty| × PriceExFee` must hold to the cent
     after the proc applies the sign. Verify by re-SELECT after the
     write:
     `ROUND(ABS(Value) - ABS(Quantity)*PriceExFee, 2) = 0`.
   - Keep `Quantity` (absolute) as the loader stored it — the swap ratio
     is the real event; do NOT recompute it from any other field.
6. `Status = 'UPDATED'`.
7. `AgentCheck`:
   ```
   fix <YYYY-MM-DD>: FII TROCA DE NOME reclassification -
   loader tagged as <BUY|SELL> at peg-price R$<peg>/unit creating phantom
   cash impact; TROCA DE NOME is a pure custody swap (<qty_sum>
   <deliver_tickers> → <qty_sum> <receive_ticker>).
   TransactionType <BUY|SELL>->ASSET <RECEIPT|DELIVERY> (pipeline skips
   Step 2.5 cash side per portfolio-creator/pipeline §6.9).
   Price/PriceExFee restated to fair-market NAV R$<nav>/unit (source:
   v_AccountPosition.PriceClose on <Date-1>); Value/ValueGross restated
   to |Qty|×NAV=<value> preserving the identity so ValueTransaction
   correctly offsets ValueClose-ValueOpen (P&L per asset = 0 for a
   fair-value swap). Status <prior>->UPDATED [TROCA-NOME]
   ```
8. `execute_procedure(procedure='Portfolio.AccountTransaction_Update', cmd='U', params={…})`.

## Post-fix verification

After all rows in the cluster are written, re-fetch each pk and verify:

- `Quantity`, `PriceExFee`, `Price`, `ValueGross`, `Value` all carry the
  correct sign per §sign-convention (see [`ayunit://docs/transaction/types`](ayunit://docs/transaction/types)).
- Identity `ROUND(ABS(Value) − ABS(Quantity) × PriceExFee, 2) = 0` holds
  on every row.
- After recompute, each participant's `Portfolio.v_AccountPosition.PnlExGrossUp`
  on the trade date is near zero (small residuals are ok if the market
  prices weren't exactly aligned; anything > ~1% of position value is
  suspicious).

## AgentCheck tag

`[TROCA-NOME]`.

## Known limitation — `AvgPrice` transfer

For a name-change swap, cost basis (`AvgPrice`) *ideally* transfers from
the delivered tickers to the received ticker weighted by delivered
notional. Per pipeline §6.3, only `BUY` updates `AvgPrice` — `ASSET
RECEIPT` does not. So the received quotas' `AvgPrice` behaviour depends
on whether the received ticker already had prior holdings:

- **Received-side had prior holdings** (`QuantityOpen > 0`) — the prior
  `AvgPrice` is carried forward per pipeline §6.3 "No trade that day →
  carried forward". The new quotas inherit the prior cost basis, which
  is usually close-enough for internal consistency.
- **Received-side is a new lot** (`QuantityOpen = 0`) — no prior
  `AvgPrice` exists, and `ASSET RECEIPT` won't set one. The received
  quotas end up with `AvgPrice = NULL` or `0`, breaking cost-basis on
  any future SELL.

If the received-side is a new lot, log the affected asset codes in the
run report so the analyst can back-fill `AvgPrice` manually
(`inception-position` skill or a targeted UPDATE with the weighted-avg
cost of the delivered tickers).

## Known limitation — real value drop on unfair-ratio consolidations

If the swap ratio doesn't match the fair-value ratio (e.g. the
delivered classes traded at premium and the received class trades at
par), the aggregate `AP_TotalPosition` will legitimately drop by the
value delta on the trade date. Per-asset `PnlExGrossUp` will be 0
(pipeline treats the swap as fair-value on the row it sees), but the
total-value drop shows in the account roll-up.

This is **economically correct** — the account holder genuinely lost
value on the consolidation. Do not "fix" the P&L to show the loss on
one of the assets; the loss belongs to the day's total, not to one
constituent.

## What this is NOT

- **Not for `TROCA` variants other than name-change.** BTG uses several
  `launchType` codes for corporate actions: `TROCA DE NOME` (this recipe),
  `DIREITO DE SUBSCRIÇÃO`, `DESDOBRAMENTO`, `AGRUPAMENTO`. Only handle
  `TROCA DE NOME` here. Others need dedicated recipes or human review.
- **Not for cross-instrument swaps.** If the cluster has assets of
  materially different `AssetGroup` (e.g. bond → equity), do **not** apply.
  Escalate — a corporate action of that shape needs analyst review.
- **Not for PENDING clusters.** If all rows in the cluster are `PENDING`,
  fall through to `pending-revalidate` first — the loader may still resolve
  them correctly on re-invocation.

## Real-world example (verified 2026-07-15)

Account `005132370`, BTG onshore, 2026-07-08:

**Raw payload from BTG** (peg-ratio prices):

| pk | Original TT | Ticker | Qty | Value (before) | Peg price |
|---|---|---|---:|---:|---:|
| 96846 | BUY | CDHY16 | 101 | −R$101,000 | R$1,000 |
| 96847 | SELL | CDHY19 | 31 | +R$0.31 | R$0.01 |
| 96848 | SELL | CDHY22 | 70 | +R$0.70 | R$0.01 |

Cluster check: 1 BUY + 2 SELL, `abs_qty_sum(BUY) = 101 = 31 + 70 = abs_qty_sum(SELL)`. ✓

`identify_asset` on CDHY16, CDHY19, CDHY22 all resolve to `Description = "FII CDHY REC"` — same fund, three ticker classes being consolidated.

**Fair-market NAVs resolved from `v_AccountPosition.PriceClose` on 2026-07-07** (last day the delivered tickers had positions):

| Ticker | NAV | Source |
|---|---:|---|
| CDHY16 | R$1,000.00 | BTG (bond-like class at par) |
| CDHY19 | R$1,120.85 | BTG |
| CDHY22 | R$1,026.90 | BTG |

**Applied fix** (all three rows, `[TROCA-NOME]` tag):

| pk | TT | Qty | PriceExFee | Value / ValueGross | Sign applied by proc |
|---|---|---:|---:|---:|---|
| 96846 | ASSET RECEIPT | 101 (abs) | 1000 | 101,000 (abs) | Qty +101, Value −101,000 |
| 96847 | ASSET DELIVERY | 31 (abs) | 1120.85 | 34,746.35 (abs) | Qty −31, Value +34,746.35 |
| 96848 | ASSET DELIVERY | 70 (abs) | 1026.90 | 71,883.00 (abs) | Qty −70, Value +71,883.00 |

Identity check post-write:
- CDHY16: `|-101,000| − |101| × 1000 = 0` ✓
- CDHY19: `|+34,746.35| − |-31| × 1120.85 = 0` ✓
- CDHY22: `|+71,883.00| − |-70| × 1026.90 = 0` ✓

**Post-recompute** — `PnlExGrossUp` on 2026-07-08 per asset:
- CDHY16: 619,000 − 518,000 + (−101,000) = **0** ✓
- CDHY19: 0 − 34,746.35 + 34,746.35 = **0** ✓
- CDHY22: 0 − 71,883.00 + 71,883.00 = **0** ✓

BRL cash delta: **+R$0.32** (from the peg-price artifact in RawTransaction; cosmetic). AP_TotalPosition dropped by R$5,629.35 across the 3 assets — the legitimate value loss from consolidating premium-priced classes into a par-priced one.

## Prior-fix regression (learned via failure)

An earlier version of this recipe (v0.2.4) set `Value = ValueGross = 0`
on all three rows, keeping the peg prices from the raw payload. That
approach:

- **Broke the identity** (`Value = 0 ≠ |Qty| × Price = 101,000` on the
  receive side).
- Set `ValueTransaction = 0` on each asset that day, so
  `PnlExGrossUp = ValueClose − ValueOpen + 0` computed a **fake
  R$101,000 gain on CDHY16** and **fake R$34,746 / R$71,883 losses** on
  CDHY19 / CDHY22 — nine-figure P&L noise on the day of the swap.

The corrected recipe (this document, v0.2.5) restates the swap at
fair-market NAVs so `ValueTransaction` correctly offsets the market-value
change, giving P&L = 0 per asset (which is what a fair-value swap
economically is). The R$5,629 total value drop is the real economic
consequence of the consolidation and appears — correctly — in the
aggregate `AP_TotalPosition` rather than in per-asset P&L.
