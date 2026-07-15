# Recipe: DEPÓSITO DE COTAS E TRIBUTOS PROVISIONADOS — tax-provisioning artifact

Distinct from **come-cotas** ([`come-cotas.md`](come-cotas.md)) despite the
structural similarity (`SELL` on a fund with `Value=0`, `ValueGross>0`).

BTG delivers a `DEPÓSITO DE COTAS E TRIBUTOS PROVISIONADOS` (literally "deposit
of quotas and provisioned taxes") entry when its internal accounting reserves
a portion of the account's quotas as provisioned tax coverage. The event
**does not correspond to a real quantity reduction** — BTG's own
`CustodyPosition` snapshot does not drop on the trade date and continues
unchanged in the following weeks. Promoting these rows would push a
fictitious SELL into `AccountPosition`, breaking the reconciliation.

The correct disposition is `Status = 'IGNORED'` — the row stays for audit
but never enters position.

**Discovered via empirical observation on account 004751948, 2026-07-15.**
Three PENDING rows on 2026-05-12 (pks 60221 / 60222 / 60223) totalling
782,256.37 units of Sten Master D5 (matching the account's **entire** current
book quantity in that asset). Custody stayed at 782,256.37 units unchanged
through 2026-05-29 (the come-cotas date) — proving these DEPÓSITO events
caused no quantity reduction.

## Detection signals (all must hold)

| Signal | Test |
|---|---|
| Row is `PENDING` | `Status = 'PENDING'` |
| Description marker | `GeneralLedgerDescription = 'DEPÓSITO DE COTAS E TRIBUTOS PROVISIONADOS'` (exact) |
| Loader raw type | `RawTransaction.launchType = 'TA'` (Transferência-Aplicação) |
| Loader-classified as SELL | `TransactionType = 'SELL'` |
| No cash movement | `Value = 0` |
| Non-zero IR provision | `ValueGross > 0` (the reserved amount) |
| Asset resolved | `Asset IS NOT NULL` |
| **Asset is ACTIVE** (not deactivated) | `Global.v_Asset.Activated = TRUE` — else it's a deactivated-fund residue instead |
| Identity holds | `ROUND(ValueGross − ABS(Quantity) × PriceExFee, 2) ≈ 0` |
| **Empirical no-reduction** | Custody-side `Quantity` on the same `(Account, Custody, Asset)` **unchanged** across `Date − 1`, `Date`, and every custody snapshot up to the next come-cotas event (or up to 30 days if no come-cotas follows) |

The **empirical no-reduction** check is the definitive signal. If custody
did drop by the DEPÓSITO quantity, the event was real and this recipe does
not apply — investigate as a wrong-sign or misclassified trade instead.

### Empirical no-reduction detection query

Given a candidate `pk`:

```sql
WITH t AS (
    SELECT pk_AccountTransactionID, ClientAccount, Custody, Date, Asset,
           ABS(Quantity) AS abs_qty
    FROM Portfolio.v_AccountTransaction
    WHERE pk_AccountTransactionID = <pk>
),
custody_series AS (
    SELECT CAST(p.[Date] AS date) AS d, p.Quantity
    FROM Portfolio.v_CustodyPosition p JOIN t
      ON p.Account = t.ClientAccount AND p.Custody = t.Custody AND p.Asset = t.Asset
    WHERE CAST(p.[Date] AS date) BETWEEN DATEADD(day, -1, t.Date)
                                     AND DATEADD(day, 30, t.Date)
)
SELECT
    MIN(Quantity) AS min_qty_in_window,
    MAX(Quantity) AS max_qty_in_window,
    CAST(MAX(Quantity) - MIN(Quantity) AS decimal(18,6)) AS qty_range,
    (SELECT abs_qty FROM t)                              AS deposito_qty,
    CASE
      WHEN (MAX(Quantity) - MIN(Quantity)) < (SELECT abs_qty FROM t) * 0.5
      THEN 1 ELSE 0
    END AS confirms_no_reduction
FROM custody_series;
```

`confirms_no_reduction = 1` → the recipe applies. `qty_range` should be
much smaller than `deposito_qty` (the DEPÓSITO's proposed reduction) — in
practice for a genuine artifact, `qty_range` is near zero (custody flat)
while `deposito_qty` is large.

Threshold `0.5 × deposito_qty` accommodates normal come-cotas / rebalancing
movements without disqualifying — a true artifact stays visibly flat.

## Fix recipe — `Status = 'IGNORED'`

Standard SELECT-first-merge:

1. Fresh SELECT of every populated column.
2. Drop `AccountCurrency` / `AccountFx`.
3. Preserve `RawTransaction`.
4. Absolute values for `Quantity` / `Price` / `PriceExFee` / `Value` / `ValueGross`.
5. **Overlay only:**
   - `Status = 'IGNORED'`
   - `AgentCheck`:
     ```
     fix <YYYY-MM-DD>: tax-provisioning artifact - DEPÓSITO DE COTAS E
     TRIBUTOS PROVISIONADOS on active fund <Asset> - empirical no-reduction
     confirmed (custody qty range <qty_range> vs deposito qty <qty>);
     BTG internal accounting entry, no real quantity movement.
     Status PENDING->IGNORED to prevent phantom SELL in AccountPosition
     [PR-IGN-TAXPROV]
     ```
6. `execute_procedure(procedure='Portfolio.AccountTransaction_Update', cmd='U', params={…})`.

## AgentCheck tag

`[PR-IGN-TAXPROV]` — distinct from `[PR-IGN-DEACT]` (deactivated-fund
residue) and `[CC]` (come-cotas).

## What this is NOT

- **Not come-cotas.** Come-cotas has `COME COTAS` in the description AND
  causes a real quantity reduction on `SettlementDate`. DEPÓSITO has neither.
- **Not deactivated-fund residue.** Deactivated-fund residue requires
  `Global.v_Asset.Activated = FALSE` — this recipe requires the opposite
  (`Activated = TRUE`). Together the two recipes cover both possibilities.
- **Not a real SELL that should be promoted.** If custody-side quantity does
  drop by the DEPÓSITO's proposed amount, the empirical check fails and this
  recipe does not apply. Escalate.

## Real-world example (verified 2026-07-15)

Account `004751948`, BTG onshore. 3 PENDING rows on 2026-05-12:

| pk | Asset | Qty | PriceExFee | ValueGross | Description |
|---|---|---:|---:|---:|---|
| 60221 | Sten Master D5 | −221,736.47 | 1.30968 | R$290,404.73 | DEPÓSITO … |
| 60222 | Sten Master D5 | −468,079.99 | 1.30968 | R$613,036.93 | DEPÓSITO … |
| 60223 | Sten Master D5 | −92,439.91 | 1.30968 | R$121,067.09 | DEPÓSITO … |

Sum of proposed reductions: 782,256.37 units.

Custody `Sten Master D5` quantity on 004751948:

| Date | Custody Qty |
|---|---:|
| 2026-05-12 (trade date) | 782,256.37 |
| 2026-05-13 | 782,256.37 |
| 2026-05-14 | 782,256.37 |
| 2026-05-15 | 782,256.37 |
| 2026-05-29 (come-cotas date) | 782,256.37 |
| 2026-06-02 (come-cotas settlement) | 777,390.65 ← real reduction from come-cotas |

`qty_range` in the 17-day post-DEPÓSITO window: 0 → confirms no reduction.
`deposito_qty`: 782,256. Confirms empirical no-reduction, decisively.

Recipe applies to all three. Status: to be set to `IGNORED` with the
`[PR-IGN-TAXPROV]` tag.
