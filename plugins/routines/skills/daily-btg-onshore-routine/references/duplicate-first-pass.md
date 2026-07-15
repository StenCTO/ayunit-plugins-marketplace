# Recipe: Duplicate-first-pass on PENDING promotion

The single most damaging orchestrator failure mode is **promoting a PENDING
row that is already represented by a VALIDATED/UPDATED sibling**. Once both
enter `AccountPosition` (via the pipeline's status-gate), the position and
cash-side value double-count — cleanly reversible in the ledger but expensive
to spot post-hoc.

**This check MUST run before any leaf that would promote a PENDING** —
`pending-revalidate`, `assetrelated-fix`, `pending-position-repair`, or any
directly-applied recipe. It is the first pre-classifier in Step 3, ahead of
all others.

## Why the loader can create structural duplicates

BTG (and other custodians) sometimes deliver the **same economic event over
two different feed paths**, each with a different `GeneralLedgerType` or
`AssetCustody` framing. Verified real-world example (account 005132370,
2026-07-15):

- **pks 79071 / 79072** — loader's earlier canonical path:
  `TransactionType='GENERAL LEDGER RECEIPT'`, `GeneralLedgerType='OTHER'`,
  `AssetCustody='CDHY16'` / `CDHY19`, `AssetRelated` pre-resolved,
  `Status='UPDATED'`.
- **pks 80501 / 80502** — a later feed path for the same events:
  `TransactionType='GENERAL LEDGER RECEIPT'`,
  `GeneralLedgerType='INTEREST/DIVIDEND'`, `AssetCustody='BRL'`,
  `AssetRelated=NULL` (loader could not infer without parsing the description),
  `Status='PENDING'`.

Same `(Account, Date, Asset, Value)`. If the orchestrator promotes 80501/80502
via `assetrelated-fix` without checking for a canonical VALIDATED/UPDATED
sibling first, the account gets **double-credited** R$14,988 in book vs the
custody's single R$14,988 event — showing up as a BRL cash phantom.

## Detection query

Given a scope of PENDING pks (from §Step 3 candidate window), find any
VALIDATED/UPDATED sibling on the **same economic key**:

```sql
WITH pending AS (
    SELECT p.pk_AccountTransactionID, p.ClientAccount, p.Custody, p.Date,
           p.TransactionType, p.Asset, p.AssetRelated,
           p.Quantity, p.Value, p.ValueGross,
           p.GeneralLedgerDescription
    FROM Portfolio.v_AccountTransaction p
    WHERE p.Status = 'PENDING'
      AND p.pk_AccountTransactionID IN (<pending pks in scope>)
),
canonical AS (
    SELECT x.pk_AccountTransactionID, x.ClientAccount, x.Custody, x.Date,
           x.TransactionType, x.Asset, x.AssetRelated,
           x.Quantity, x.Value, x.ValueGross,
           x.GeneralLedgerDescription, x.Status
    FROM Portfolio.v_AccountTransaction x
    WHERE x.Status IN ('VALIDATED','UPDATED')
)
SELECT
    p.pk_AccountTransactionID AS pending_pk,
    c.pk_AccountTransactionID AS canonical_pk,
    c.Status                  AS canonical_status,
    p.Asset,
    COALESCE(p.AssetRelated, c.AssetRelated) AS AssetRelated,
    p.Value,
    p.ValueGross,
    p.Date
FROM pending p
JOIN canonical c
     ON  c.ClientAccount = p.ClientAccount
     AND c.Custody       = p.Custody
     AND c.Date          = p.Date
     AND c.TransactionType = p.TransactionType
     -- Same Asset by any resolution: direct match, or AssetRelated cross-match
     AND (
             c.Asset = p.Asset
          OR c.AssetRelated = COALESCE(p.AssetRelated, p.Asset)
          OR c.Asset = COALESCE(p.AssetRelated, p.Asset)
          OR c.AssetRelated = p.Asset
         )
     AND ABS(ABS(c.Value) - ABS(p.Value)) < 0.01
ORDER BY p.pk_AccountTransactionID;
```

The `AssetRelated` cross-match is critical for the BTG two-feed-path case: on
one row the actual asset lives in `AssetRelated` (canonical GL-RECEIPT-OTHER
pattern), on the other it needs to be parsed from the description
(GL-RECEIPT-INTEREST/DIVIDEND pattern).

## Fix recipe — `Status = 'IGNORED'` on the PENDING duplicate

The canonical row (VALIDATED/UPDATED, oldest by pk) survives. The PENDING
sibling gets `IGNORED` — never enters position, retains the audit trail:

1. Standard SELECT-first-merge on the PENDING pk (every populated column).
2. Drop `AccountCurrency` and `AccountFx` (MCP wrapper rejects otherwise).
3. Preserve `RawTransaction` verbatim.
4. Absolute values for `Quantity` / `Price` / `PriceExFee` / `Value` / `ValueGross`.
5. Overlay: `Status = 'IGNORED'`.
6. `AgentCheck` = `"REVERT/DUP <YYYY-MM-DD>: duplicates canonical pk <N> (same Account/Date/Asset via AssetRelated cross-match; canonical is <VALIDATED|UPDATED>). Status PENDING->IGNORED [DUP-REVERT]"`.
7. `execute_procedure(procedure='Portfolio.AccountTransaction_Update', cmd='U', params={…})`.

If **both** rows are PENDING (rare — usually the loader promotes one canonical), do **not** apply this recipe from the orchestrator. Fall through to `duplicate-trade-reconcile` (leaf skill) which reconciles against `Portfolio.v_CustodyPosition` and decides the survivor.

## AgentCheck tag

`[DUP-REVERT]` — distinct from every other write path. The audit query can filter this bucket to review orchestrator-applied dedups without noise from other categories.

## What this is NOT

- **Not full duplicate-trade-reconcile.** This first-pass only handles the case where a **PENDING** row duplicates an already-canonical VALIDATED/UPDATED sibling. Duplicate clusters of two VALIDATED rows (both promoted, both wrong) belong to `duplicate-trade-reconcile`, which reads `CustodyPosition` for ground truth.
- **Not price/quantity comparison.** The match is on `(Account, Custody, Date, TransactionType, Asset-or-AssetRelated, ABS(Value))`. Quantity is not in the key because two feeds can round differently while representing the same event.
- **Not for structurally-different but economically-similar trades.** If the two rows have different TransactionTypes (e.g. one BUY and one ASSET RECEIPT), that's a **misclassification** case (see `troca-de-nome.md`), not a duplicate.

## Real-world example (verified 2026-07-15)

Account `005132370`, BTG onshore. Post-loader state:

- pk 79071 (UPDATED, 2026-06-08, GL RECEIPT OTHER, AssetRelated=CDHY16, Value=14141.73)
- pk 79072 (UPDATED, 2026-06-08, GL RECEIPT OTHER, AssetRelated=CDHY19, Value=846.31)
- pk 80501 (PENDING, 2026-06-08, GL RECEIPT INTEREST/DIVIDEND, description "REC - CDHY19", Value=846.31)
- pk 80502 (PENDING, 2026-06-08, GL RECEIPT INTEREST/DIVIDEND, description "F11 - CDHY16", Value=14141.73)

Detection matched (80501 ↔ 79072, 80502 ↔ 79071) on the AssetRelated cross-match and Value equality. Fix: IGNORE 80501 and 80502, keep 79071/79072 as canonical.

**Learned via failure**: the orchestrator initially ran `assetrelated-fix` on 80501/80502 **before** the duplicate check, double-counting R$14,988 into book BRL. The duplicate-first-pass now runs ahead of all leaf skills to prevent this.
