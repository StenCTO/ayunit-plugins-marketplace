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
correct disposition is to reclassify the `TransactionType`:

- Receive-side `BUY` → **`ASSET RECEIPT`**
- Deliver-side `SELL` → **`ASSET DELIVERY`**

Per [`ayunit://docs/portfolio-creator/pipeline`](ayunit://docs/portfolio-creator/pipeline) §6.9,
`ASSET RECEIPT` / `ASSET DELIVERY` do **not** generate the Step-2.5 cash
side. Zero cash impact, quantity moves only.

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

## Fix recipe

For **each** row in the cluster, apply a single SELECT-first-merge `@CMD='U'`:

1. Fresh SELECT of every populated column of the row.
2. Drop `AccountCurrency` and `AccountFx`.
3. Preserve `RawTransaction` verbatim.
4. Absolute values for `Quantity` / `Price` / `PriceExFee` / `Value` / `ValueGross`.
5. **Overlay:**
   - `TransactionType`: `BUY → 'ASSET RECEIPT'`, `SELL → 'ASSET DELIVERY'`.
   - `Value = 0` — no cash impact (belt-and-suspenders; the pipeline
     already skips the cash side for these types, but a residual `Value`
     contaminates `ValueTransaction` per pipeline §6.4 and creates a
     spurious P&L attribution on the shock day).
   - `ValueGross = 0` — same reasoning; also protects `AvgPrice` from a
     phony recalculation, though per pipeline §6.3 only `BUY` updates
     `AvgPrice` (so this is precaution).
   - Keep `Quantity`, `Price`, `PriceExFee` as-is (the peg prices are the
     accurate record of what BTG delivered, and the quantity moves are the
     event we want to reflect).
6. `Status = 'UPDATED'`.
7. `AgentCheck`:
   ```
   fix <YYYY-MM-DD>: FII TROCA DE NOME reclassification -
   loader tagged as <BUY|SELL> at peg-price R$<price>/unit creating phantom
   cash impact; TROCA DE NOME is a pure custody swap
   (<qty_sum> <deliver_tickers> → <qty_sum> <receive_ticker>);
   TransactionType <BUY|SELL>->ASSET <RECEIPT|DELIVERY> (pipeline skips
   Step 2.5 cash side per portfolio-creator/pipeline §6.9);
   Value/ValueGross zeroed to avoid ValueTransaction contamination;
   Status <prior>->UPDATED [TROCA-NOME]
   ```
8. `execute_procedure(procedure='Portfolio.AccountTransaction_Update', cmd='U', params={…})`.

## AgentCheck tag

`[TROCA-NOME]`.

## Known limitation — `AvgPrice` transfer

For a name-change swap, cost basis (`AvgPrice`) should transfer from the
delivered tickers to the received ticker weighted by delivered value.
This recipe does **not** do that — after the fix, the received ticker's
`AvgPrice` will be whatever the pipeline computes on `ASSET RECEIPT`, which
per pipeline §6.3 is unchanged by anything other than `BUY`. So the
received quotas may end up with `AvgPrice = 0` or `NULL`, breaking future
SELL cost-basis calculations.

This is a **known trade-off** — the alternative (leaving the phantom cash
debit in place) is a worse operational problem. If the received asset is
ever sold, the analyst may need to manually adjust `AvgPrice` on the
inception rows via the `inception-position` skill or a targeted UPDATE.
Log the affected asset codes in the run report so the analyst can catch
this on the next cost-basis review.

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

| pk | Original TT | Ticker | Qty | Value (before) | Peg price |
|---|---|---|---:|---:|---:|
| 96846 | BUY | CDHY16 | 101 | −R$101,000 | R$1,000 |
| 96847 | SELL | CDHY19 | 31 | +R$0.31 | R$0.01 |
| 96848 | SELL | CDHY22 | 70 | +R$0.70 | R$0.01 |

Cluster check: 1 BUY + 2 SELL, `abs_qty_sum(BUY) = 101 = 31 + 70 = abs_qty_sum(SELL)`. ✓

`identify_asset` on CDHY16, CDHY19, CDHY22 all resolve to `Description = "FII CDHY REC"` — same fund, three ticker classes being consolidated.

Applied: reclassify to ASSET RECEIPT (96846) / ASSET DELIVERY (96847, 96848), Value=0 / ValueGross=0 on all three, `[TROCA-NOME]` tag.

Post-recompute: BRL cash delta collapsed from −R$86,010 to +R$0.32 (rounding). Position quantities in CDHY16/CDHY19/CDHY22 now match custody.
