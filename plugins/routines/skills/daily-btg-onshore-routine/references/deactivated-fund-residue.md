# Recipe: Deactivated-fund residue on `PENDING`

When a BR fund is closed / merged / retired, the custody feed sometimes sends a
**trailing entry** for the fund weeks after the account stopped holding it (or
for an account that never held it at all). The loader can still resolve the
asset via CNPJ against `Global.Asset`, but the resolution finds a row whose
`Activated = FALSE` — a **deactivated asset**. The loader then lands the row
`PENDING` because it can't derive a coherent Price / Value.

Promoting these rows (via `pending-revalidate` or `pending-position-repair`)
would land a **phantom trade** in `AccountPosition`: the calculated position
would carry a Quantity for an asset the account has no history of holding.
The correct disposition is `Status = 'IGNORED'` — the row stays in the ledger
for audit, but is excluded from `AccountPosition` per the pipeline's
status-gate rule ([`portfolio-creator/pipeline`](ayunit://docs/portfolio-creator/pipeline) §2).

## Symptom pattern (all conditions must hold)

| Condition | Test |
|---|---|
| Row is `PENDING` with `missing: Price` | `SystemCheck LIKE '%missing: %Price%'` |
| Asset is resolved on the row | `Asset IS NOT NULL` |
| Asset is deactivated in the master | `Global.v_Asset.Activated = 0` for that `Asset` |
| Account has no history in the asset | `Portfolio.v_AccountPosition` returns 0 rows for `(Account, Asset)` across all time |
| Asset is absent from custody on the trade date | `Portfolio.v_CustodyPosition` shows no matching row for `(Account, Asset)` on `Date` |
| Asset is absent from custody at the latest snapshot | `Portfolio.v_CustodyPosition` shows no matching row for `(Account, Asset)` on the account's `MAX([Date])` |
| No other transactions on the asset for this account | `Portfolio.v_AccountTransaction` returns exactly 1 row for `(ClientAccount, Asset)` — the PENDING itself |

If **any** condition fails, this recipe does not apply — hand back to the
appropriate leaf. In particular:

- Asset held historically but currently zero → look for a wrong-signed / missing SELL first (`pending-position-repair` or `duplicate-trade-reconcile`).
- Asset present in custody at the latest snapshot → the account genuinely holds it now; this is not residue.
- Asset present in custody on the trade date → the fund event is real (not a phantom); investigate as a normal trade.
- Multiple transactions exist → the residue theory doesn't fit; the fund event has a real history and needs analyst review.

**Why two custody checks and not one time window.** The BTG custody feed can
briefly reflect a fund closure for a few days between the trade and the final
purge (verified: pk 59421 showed `STEN MFO D30` in custody 2026-05-12 to
2026-05-15 while BTG was working the closure event, then it disappeared). A
±30-day window catches those transient rows and false-negatives the detector.
The two-point check (trade date + latest snapshot) is robust: transient rows
between them don't disqualify.

## Detection query (SELECT-only, orchestrator-safe)

Given a `PENDING` pk, confirm all seven conditions:

```sql
WITH t AS (
    SELECT pk_AccountTransactionID, ClientAccount, Custody, Date, Asset,
           CAST(SystemCheck AS varchar(1000)) AS SystemCheck_txt
    FROM Portfolio.v_AccountTransaction
    WHERE pk_AccountTransactionID = <pk>
),
asset_check AS (
    SELECT a.Asset, a.Activated
    FROM Global.v_Asset a JOIN t ON t.Asset = a.Asset
),
ap_hist AS (
    SELECT COUNT(*) AS ap_rows
    FROM Portfolio.v_AccountPosition p JOIN t
      ON p.Account = t.ClientAccount AND p.Custody = t.Custody AND p.Asset = t.Asset
),
latest_cp AS (
    SELECT CAST(MAX([Date]) AS date) AS latest_date
    FROM Portfolio.v_CustodyPosition p JOIN t
      ON p.Account = t.ClientAccount AND p.Custody = t.Custody
),
cp_on_trade AS (
    SELECT COUNT(*) AS cp_trade_rows
    FROM Portfolio.v_CustodyPosition p JOIN t
      ON p.Account = t.ClientAccount AND p.Custody = t.Custody AND p.Asset = t.Asset
    WHERE CAST(p.[Date] AS date) = CAST(t.Date AS date)
),
cp_on_latest AS (
    SELECT COUNT(*) AS cp_latest_rows
    FROM Portfolio.v_CustodyPosition p JOIN t
      ON p.Account = t.ClientAccount AND p.Custody = t.Custody AND p.Asset = t.Asset
    JOIN latest_cp lcp ON CAST(p.[Date] AS date) = lcp.latest_date
),
tx_hist AS (
    SELECT COUNT(*) AS tx_rows
    FROM Portfolio.v_AccountTransaction x JOIN t
      ON x.ClientAccount = t.ClientAccount AND x.Custody = t.Custody
     AND (x.Asset = t.Asset OR x.AssetRelated = t.Asset)
)
SELECT
    t.pk_AccountTransactionID,
    CASE WHEN t.SystemCheck_txt LIKE '%missing: %Price%' THEN 1 ELSE 0 END AS pending_missing_price,
    CASE WHEN t.Asset IS NOT NULL                        THEN 1 ELSE 0 END AS asset_resolved,
    CASE WHEN asset_check.Activated = 0                  THEN 1 ELSE 0 END AS asset_deactivated,
    CASE WHEN ap_hist.ap_rows = 0                        THEN 1 ELSE 0 END AS no_position_history,
    CASE WHEN cp_on_trade.cp_trade_rows = 0              THEN 1 ELSE 0 END AS not_in_custody_on_trade_date,
    CASE WHEN cp_on_latest.cp_latest_rows = 0            THEN 1 ELSE 0 END AS not_in_custody_currently,
    CASE WHEN tx_hist.tx_rows = 1                        THEN 1 ELSE 0 END AS only_this_transaction
FROM t
LEFT JOIN asset_check ON 1=1
LEFT JOIN ap_hist ON 1=1
LEFT JOIN cp_on_trade ON 1=1
LEFT JOIN cp_on_latest ON 1=1
LEFT JOIN tx_hist ON 1=1;
```

All seven flags = 1 → apply the fix below. Any 0 → do not apply; route elsewhere.

## Fix recipe — `Status = 'IGNORED'`

Standard SELECT-first-merge, same guardrails as every write in the account-
transaction plugin:

1. **SELECT** every populated column of the row (see
   [`ayunit://docs/transaction/procedure`](ayunit://docs/transaction/procedure)
   for the full param list).
2. **Drop** `AccountCurrency` and `AccountFx` — the procedure computes them
   and the MCP wrapper rejects payloads that include them (400).
3. **Preserve** `RawTransaction` verbatim — it is the original custody payload
   and the analyst re-reads it during review.
4. **Pass absolute values** for `Quantity` / `Price` / `PriceExFee` / `Value` /
   `ValueGross`; the procedure applies the sign from `TransactionType`.
5. **Overlay two fields only:**
   - `Status = 'IGNORED'`
   - `AgentCheck = "fix <YYYY-MM-DD>: deactivated-fund residue - Asset=<X> is Activated=false in Global.Asset; account has no history in this asset and it does not appear in custody around the trade date; Status PENDING->IGNORED to prevent phantom position [PR-IGN-DEACT]"`
6. `execute_procedure(procedure='Portfolio.AccountTransaction_Update', cmd='U', params={…})`.

`IGNORED` never enters `AccountPosition` / `Share` (per the pipeline
status-gate). No PortfolioCreator recompute is required after this fix.

## AgentCheck tag

Use `[PR-IGN-DEACT]` — distinct from `[PR]` (pending-revalidate) and
`[PR-POS]` (pending-position-repair) so the audit's differ can filter
this specific disposition path.

## Real-world example (verified 2026-07-15)

Account `001635274`, BTG onshore. `pk 59421`, dated 2026-05-11:

- `TransactionType = SELL`, `Asset = STEN MFO D30` (CNPJ 57601370000100)
- `AssetCustody = "Grit FICFIM CrPr"`, `GeneralLedgerDescription = "DEPÓSITO DE COTAS E TRIBUTOS PROVISIONADOS"`, `launchType = "TA"`
- `Quantity = -718,650.63`, `PriceExFee = 1.24853`, `ValueGross = 897,255.12`, `Value = 0`
- `SystemCheck`: "Asset identified: 'STEN MFO D30' … Revalidation: Status remains PENDING (missing: Price)"
- `Global.v_Asset.Activated = FALSE` for `STEN MFO D30`
- `AccountPosition` history for `(001635274, STEN MFO D30)`: **0 rows across all time**
- `CustodyPosition` on trade date 2026-05-11: **absent**
- `CustodyPosition` on latest snapshot 2026-07-09: **absent**
- `CustodyPosition` **transient** rows 2026-05-12 to 2026-05-15 (qty 718,650.63, matching the PENDING) — closure-processing artifacts; the two-point check correctly ignores these
- `AccountTransaction` count for the asset: **1** (this row)

Fix applied: `Status = 'IGNORED'` with the `[PR-IGN-DEACT]` AgentCheck tag.

## What this is not

- **Not come-cotas.** Come-cotas rows describe an *actual* fund position paying
  in-kind tax; the account genuinely holds quotas and surrenders some. This
  recipe applies only when the account has **no** history in the asset. If
  the fund is active or the account has ever held quotas, use the come-cotas
  recipe in `CLAUDE.md §6` instead.
- **Not a duplicate-trade case.** No mirror row exists to reconcile against.
- **Not a Bucket 3-A/3-B/3-C revalidate.** The blocker isn't "master data
  caught up" — the master data actively says the asset is deactivated.
