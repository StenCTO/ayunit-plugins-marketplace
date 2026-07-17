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

## Mode B — Account-wide cash-inclusive sweep (added 2026-07-15)

Mode A (above) catches PENDING duplicates of a canonical VALIDATED/UPDATED
row. It **misses** the sibling pattern where a re-load creates two
`VALIDATED/UPDATED` rows for the same event on cash-only trades — because
those never surface as a `QtyMismatchAssets` signal (they touch `BRL`, not
a security). Verified 2026-07-15 on account 004434113: pk 48814 (WITHDRAW
BRL 50050.10, originally PENDING then promoted this morning) had a sibling
pk 52321 (WITHDRAW BRL 50050.10, VALIDATED, `InputDate` 2026-05-01) from
the same re-load batch that also duplicated the SULAMEX SELL pair
52319/52320. The SULAMEX duplicates surfaced via qty mismatch; the
WITHDRAW duplicate stayed hidden until the analyst caught it manually.

### Mode B detection query

Sweep every in-window row on the account and cluster by economic key:

```sql
SELECT CAST(Date AS date) AS dt, TransactionType, Asset,
       ABS(Value) AS abs_val, GeneralLedgerDescription,
       COUNT(*) AS n,
       STRING_AGG(CAST(pk_AccountTransactionID AS varchar), ',') AS pks,
       STRING_AGG(Status, ',') AS statuses,
       MIN(InputDate) AS earliest_input,
       MAX(InputDate) AS latest_input
FROM Portfolio.v_AccountTransaction
WHERE Custody = @custody
  AND ClientAccount = @acct
  AND Status IN ('VALIDATED','UPDATED')
  AND CAST(Date AS date) BETWEEN @lock_plus_1 AND @end_date
GROUP BY CAST(Date AS date), TransactionType, Asset,
         ABS(Value), GeneralLedgerDescription
HAVING COUNT(*) > 1
ORDER BY dt, TransactionType;
```

### Real-duplicate vs coincident-value discriminator

For each returned group, apply this test — do **not** IGNORE unless it
proves out:

| Signal | Real re-load duplicate | Legitimate distinct events |
|---|---|---|
| `latest_input − earliest_input` | days or weeks apart | seconds (same feed batch) |
| PK cluster | one row from original batch, later row in a distant re-load block (e.g. 48xxx original vs 52xxx re-load) | strictly sequential (48688, 48691) |
| `GeneralLedgerDescription` verbatim | identical | identical |
| `Value` magnitude | identical to the cent | often near-identical but not exact |
| **Custody delta corroboration** | day-over-day `CustodyPosition` change on the affected `Asset` matches **one** row's impact, not both | matches **both** rows summed |

**All five** signals must align for Mode B to auto-IGNORE. If custody is
missing or the group has near-identical values (two different bond
coupons paying similar cents on the same day — verified 2026-07-15 pairs
48688/48691 JUROS R$167.28 and 48689/48690 AMORTIZAÇÃO R$166.43 on
004434113 which were distinct bond coupons, not duplicates), **do not
touch them** — report as investigation candidates.

### Fix recipe (Mode B) — two-phase IGNORE→verify→DELETE

Unlike Mode A (which leaves the PENDING duplicate as an `IGNORED` audit
row permanently), Mode B duplicates are safe to **delete** after the
recompute proves positions reconcile — no reason to keep a phantom
re-loaded row. Apply the two-phase pattern from
`duplicate-trade-reconcile`:

1. **Phase 1 — U to IGNORED (autonomous)**: SELECT-first-merge the
   later-inserted row (higher `InputDate`), overlay `Status = 'IGNORED'`,
   set `AgentCheck = "REVERT/DUP <YYYY-MM-DD>: feed-reload duplicate of
   canonical pk <N> (InputDate <old> vs <new>); Phase 1 -> IGNORED,
   DELETE after recompute verifies [DUP-REVERT]"`.
2. **Recompute** `calculate_portfolio(end_date = <caller's end_date>, ...)`.
3. **Verify**: Step 3.5 re-diff shows the previously-doubled asset (or
   cash balance) now matches custody.
4. **Phase 2 — D delete (autonomous)**: `CMD='D'` on the same pk. Do not
   pause for user confirmation — verification is the safety gate.

If the verify at step 3 does not resolve, restore `Status =
'VALIDATED'` on the paused row (single U), recompute again, and report
the case for human review.

## What this is NOT

- **Not full duplicate-trade-reconcile.** This first-pass only handles two
  cases: (Mode A) a **PENDING** row duplicates an already-canonical
  VALIDATED/UPDATED sibling; (Mode B) an account-wide sweep for feed-reload
  duplicate clusters on any status. Broader duplicate clusters — mixed
  status, restated NAVs, multi-day settlement patterns — belong to
  `duplicate-trade-reconcile`, which reads `CustodyPosition` for ground
  truth.
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
