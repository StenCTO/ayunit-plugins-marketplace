---
name: pending-revalidate
description: "Use when the user wants to re-run the loader validators on Portfolio.AccountTransaction rows that were left PENDING because a prerequisite was not yet in master data (asset unmapped in Portfolio.AssetCustody, missing price in AssetData, missing quantity derivable from Value/Price), and the prerequisite is NOW available. This skill takes a scope (pk list, account/custody window, or a filter), classifies each PENDING row's blocker from the loader's own SystemCheck diagnostic, verifies the blocker has cleared today, lock-gates against CheckedDate, and re-invokes Portfolio.AccountTransaction_Update @CMD='U' via a SELECT-first-merge so the procedure's built-in auto-validators (auto-match Asset, auto-fill Price, auto-fill Quantity) fire again and promote the row. Custody-agnostic. Sibling of transaction-workday-audit (Check 2 Step 3 is the detector; this skill is the fix — the SystemCheck classifier lives there), duplicate-trade-reconcile, assetrelated-fix, compromissada-fix. Trigger whenever the user says re-validate / re-processar / promote PENDING / 'the mapping was added, now retry the tape' / 'try to fix these stuck rows' — or when transaction-workday-audit hands a pk list off to this skill."
---

# Re-validate stuck `PENDING` rows against current master data

You are the orchestrator for **re-running the loader validators** on
`Portfolio.AccountTransaction` rows that got stuck `PENDING` because their
prerequisites weren't in master data at load time. The loader **does not
retroactively re-process** the tape when the prerequisite lands later; those
rows stay `PENDING` until something re-triggers `Portfolio.AccountTransaction_Update`
on them.

The procedure has built-in auto-validators (CLAUDE.md §8, verified against
`get_procedure_detail`):

1. Auto-match `Asset` from `AssetCustody` / `CustodyIdentifier` if `Asset` empty.
2. Auto-fill `Price` from `AssetData.v_Price` when `Price`, `Value` and
   `ValueGross` are **all** 0/NULL and `Quantity ≠ 0`.
3. Auto-fill `Quantity` from `Value` when `Quantity` / `Price` are empty.
4. Compute `Price = ABS(Value / (Quantity × ContractSize))` and fall back
   `PriceExFee` to `Price` when passed as 0/NULL.

Re-invoking `@CMD='U'` on a PENDING row makes those fire again. If master data
has caught up (mapping added, price loaded, quantity derivable), the row
resolves and promotes.

**Scope discipline.** This skill is **custody-agnostic**. It classifies rows
by the loader's `SystemCheck` grammar, which is the same across every feed.
It writes only when the blocker is provably cleared **today** — never on
"maybe."

## Inputs

The caller supplies scope in one of three shapes:

1. **Explicit pk list** (preferred, the audit hand-off shape) — an
   `IN (…)` list of `pk_AccountTransactionID`. No implicit widening.
2. **Filter over `Portfolio.v_AccountTransaction`** — any `(Account, Custody,
   Date window)` combination. The skill **only** touches rows in that filter
   whose `Status = 'PENDING'`.
3. **Bucket restriction** (optional) — `3-A` (Asset), `3-B` (Price), `3-C`
   (Quantity). Default: all three. Never `3-D` (that's `assetrelated-fix`)
   and never `3-Z-*` (out of scope by design).

Echo the resolved scope + bucket restriction at the start of every report.

## Reference resources (read on demand)

| Resource | Read when… |
|---|---|
| [`ayunit://docs/transaction/fixes`](ayunit://docs/transaction/fixes) | **First.** Recipe **R1** is the stuck-PENDING completion pattern; universal write guardrails (SELECT-first-merge, drop `AccountCurrency` / `AccountFx`, absolute values, preserve `RawTransaction`, `AgentCheck`). |
| [`ayunit://docs/transaction/procedure`](ayunit://docs/transaction/procedure) · [`types`](ayunit://docs/transaction/types) | `AccountTransaction_Update` params, the auto-validator suite that this skill relies on, and the `Status` lifecycle. |
| [`ayunit://docs/checkeddate/usage`](ayunit://docs/checkeddate/usage) | **Before any write.** The lock contract: the proc rejects a write when an `Activated=1` `v_CheckedDate` exists for `(Account, Custody)` and the row's `Date` **or** `SettlementDate` ≤ the lock date. |
| [`ayunit://docs/portfolio-creator/pipeline`](ayunit://docs/portfolio-creator/pipeline) | Why promoting these rows matters: only `VALIDATED` / `UPDATED` rows with a resolved asset reach `AccountPosition`. |

## Tools you call directly

- `execute_select_query` — every read (scope, blocker resolvability, lock lookup, verification).
- `execute_batch(items=[… cmd='U' …], dry_run=…)` — the write path, atomic.
  `@CMD='U'` is non-destructive, so `allow_destructive` is **not** required.
- `execute_procedure(procedure='Portfolio.AccountTransaction_Update', cmd='U', …)`
  — single-row alternative when the scope is one pk.
- `get_procedure_detail` — confirm `AccountTransaction_Update` params on the first run of a session; never guess.

## The revalidation cycle

### 1 — Read the current state (single query, all pks)

```sql
SELECT pk_AccountTransactionID, Date, SettlementDate, ClientAccount, Broker,
       Custody, TransactionType, GeneralLedgerType, GeneralLedgerDescription,
       Currency, AssetCustody, CustodyIdentifier, Asset, AssetRelated,
       Quantity, Price, PriceExFee, ValueGross, Value, Status,
       CAST(SystemCheck AS varchar(1000)) AS SystemCheck_txt,
       CAST(Obs         AS varchar(500))  AS Obs_txt,
       CAST(RawTransaction AS varchar(max)) AS RawTransaction_txt
FROM Portfolio.v_AccountTransaction
WHERE pk_AccountTransactionID IN (…);
```

Skip any row whose `Status <> 'PENDING'` (already resolved by another path).
Report them under **Skipped — already resolved**.

### 2 — Classify each row's blocker

Same grammar as `transaction-workday-audit` Check 2 Step 3. Read the `SystemCheck`
text (verified production examples):

| Bucket | `SystemCheck` signal (order matters — check `AssetRelated` before generic `Asset`) |
|---|---|
| **3-A · Asset** | `missing: Asset` (often `Fail to get Asset Register; …`) — or `SystemCheck` NULL + `Asset` NULL + identifier present (rare) |
| **3-B · Price** | `missing: Price` (`Asset` typically already resolved) |
| **3-C · Quantity** | `missing: Quantity` |
| **3-D · AssetRelated** | `missing: AssetRelated` — **not this skill**, hand back to caller with `assetrelated-fix` pointer |
| **3-Z** | come-cotas (Obs `LIKE '%COME COTAS%'`), `Invalid TransactionType`, `DEBIT CARD`, three-way missing (`Asset` + `Quantity` + `Price`), `SOURCE: FUND_ACCOUNT_STATEMENT` etc. — **not this skill** |

3-D and 3-Z rows: **do not write**. Report them.

### 3 — Verify the blocker is cleared *today* (per bucket, one query each)

**3-A** — the identifier now resolves:

```sql
SELECT p.pk_AccountTransactionID, ac.Asset AS ResolvedAsset
FROM (…scope…) p
JOIN Portfolio.v_AssetCustody ac
     ON  ac.Custody = p.Custody
     AND (ac.TickerCustody  = p.AssetCustody
       OR ac.TickerCustody2 = p.AssetCustody
       OR ac.TickerCustody  = p.CustodyIdentifier
       OR ac.TickerCustody2 = p.CustodyIdentifier);
```

Reject with **Reported — blocker still active** if:
- zero rows come back (mapping still missing → `asset-register` first), or
- more than one distinct `ResolvedAsset` for a single pk (ambiguous mapping,
  master-data conflict → surface both, do not pick).

**3-B** — a price now exists:

```sql
SELECT p.pk_AccountTransactionID, pr.Value AS ResolvedPrice
FROM (…scope…) p
JOIN AssetData.v_Price pr ON pr.Asset = p.Asset AND pr.Date = p.Date;
```

Reject if zero rows (price feed still catching up).

**3-C** — Quantity is derivable: `Value IS NOT NULL AND Price IS NOT NULL AND
Price <> 0`. Reject otherwise.

### 4 — Lock-gate

```sql
SELECT Account, Custody, Date, Activated
FROM Portfolio.v_CheckedDate
WHERE Account IN (…) AND Activated = 1
ORDER BY Account, Date DESC;
```

- **>1 `Activated=1` row for the same `(Account, Custody)`** — proc's scalar
  subquery raises (error 512). **Skip** the whole account; report as
  **Skipped — broken lock**. Do not attempt to fix from this skill.
- **Row's `Date` or `SettlementDate` ≤ lock date** — report as
  **Lock-blocked**. A CheckedDate move is a user decision (recoil cycle);
  emit a copy-paste `EXEC Portfolio.CheckedDate_Update …` note but do **not**
  execute it.

### 5 — Build the merged params (SELECT-first-merge) and write

For each writable row:

1. Start from **every populated column** you read in step 1.
2. **Drop `AccountCurrency` and `AccountFx`** — the proc rejects the whole
   payload otherwise (generic 400 before SQL runs).
3. **Preserve `RawTransaction`** — pass the full JSON string back intact.
4. **Pass absolute values** for `Quantity` / `Price` / `PriceExFee` / `Value`
   / `ValueGross`. The proc applies the sign per §5 of CLAUDE.md.
5. **Bucket-specific overlay:**
   - **3-A** — pass `Asset = <ResolvedAsset>` explicitly (belt-and-suspenders:
     the auto-matcher would derive it, but making it explicit removes any
     dependency on identifier ambiguity). Leave other fields alone.
   - **3-B** — leave `Price` / `PriceExFee` at 0/NULL if that's how they came
     in **and** `Value` / `ValueGross` are also 0/NULL (so validator #2 fires
     against `AssetData.v_Price`). If `Value` is populated, pass it and let
     the proc's `Price = ABS(Value / (Quantity × ContractSize))` rule derive
     `Price` at write time.
   - **3-C** — leave `Quantity` at 0/NULL, pass `Value` and `Price`; validator
     #3 derives Quantity.
6. Set `Status = 'UPDATED'`. `UPDATED` counts like `VALIDATED` in the
   pipeline and skips the strict "Price required" check (CLAUDE.md §3), which
   protects the 3-B path when the proc's derivation lands at exactly the
   fill-in NAV.
7. Set `AgentCheck`:
   ```
   fix YYYY-MM-DD: PENDING re-validated - <bucket 3-A|3-B|3-C>: <what changed> (blocker cleared: <mapping added / price now available / quantity derivable>); Status PENDING->UPDATED [PR]
   ```
8. Submit as `execute_batch(items=[…], dry_run=true)` **first**. Confirm
   `failed_index` is null. Then re-submit `dry_run=false`. The batch is
   atomic — one failure rolls back the lot, which is the correct semantics
   here (don't half-promote a set the analyst thinks of as one hand-off).

For a single-pk scope, `execute_procedure` is fine too; use `execute_batch`
whenever there is more than one row.

### 6 — Verify

Re-SELECT every written pk with the step-1 query:

- `Status` should now be `UPDATED` (or `VALIDATED` if the proc promoted it).
- `Asset` should be populated (3-A), or non-zero `Price` (3-B), or non-zero
  `Quantity` (3-C).
- `SystemCheck` should either be NULL or should no longer contain the
  `missing: <field>` for the field you targeted.

Any pk still `PENDING` with the **same** missing-field diagnostic is a
**detector bug** — the resolvability probe (step 3) said yes but the proc
kept the row `PENDING`. Do not retry silently; report it so the classifier
can be fixed. Common causes: identifier text mismatch (whitespace, case),
price feed granularity, ContractSize ≠ 1 messing with the derivation.

## Report buckets

End every run with these buckets so nothing is silently dropped:

| Bucket | Meaning | Disposition |
|---|---|---|
| **Promoted** | `Status = UPDATED` (or `VALIDATED`), field populated, `AgentCheck` set | done |
| **Skipped — already resolved** | row was no longer `PENDING` at read time | done, no write |
| **Skipped — broken lock** | `(Account, Custody)` has >1 active `CheckedDate` | fix duplicate lock first (out of scope) |
| **Lock-blocked** | `Date` / `SettlementDate` ≤ active lock date | needs `CheckedDate_Update` (user-approved recoil cycle); emit paste-able `EXEC` |
| **Reported — blocker still active** | 3-A no mapping, 3-B no price, 3-C not derivable | hand off (`asset-register`, pricing team, analyst) and re-run this skill after |
| **Reported — hand-off elsewhere** | 3-D (→ `assetrelated-fix`), 3-Z-\* | not this skill's territory |
| **Reported — detector bug** | verified probe said resolvable, but the proc kept it `PENDING` | file against classifier; do not retry |

## Critical rules

- **Custody-agnostic.** Classification is `SystemCheck` text + a
  master-data probe. Never fork per custody.
- **Read the loader before you overwrite it.** `SystemCheck` is the source
  of truth for *why* — do not re-derive that from the row shape alone.
- **SELECT-first-merge, always.** `@CMD='U'` overwrites every column from
  what you pass. Omitting a populated field is data loss.
- **Drop `AccountCurrency` and `AccountFx`** on every write — the proc
  computes them.
- **Preserve `RawTransaction`** — it's the original custody payload.
- **Pass absolute values** for `Quantity` / `Price` / `PriceExFee` / `Value`
  / `ValueGross`. The proc applies the sign.
- **Lock-gate every row** before writing. Never move a `CheckedDate` from
  this skill — surface the `EXEC` and let the analyst approve.
- **Bucket 3-D and 3-Z never get written from here.** Report and hand off.
- **Always set `AgentCheck`** with the `[PR]` tag so the next session (and
  the audit's verification query) can distinguish this fix path.
- **Verify after every commit** and count any residual `PENDING` from your
  scope as a detector bug, not as a rerun target.
- **Reply in the user's language** (PT / EN) and echo the resolved scope + bucket restriction.

## When unsure

- **The pk list mixes buckets.** Fine — process each bucket in its own
  branch, then commit the batch atomically. Do not filter to one bucket
  silently; the analyst asked about all of them.
- **A 3-A row's identifier resolves to two different Assets** in
  `Portfolio.v_AssetCustody`. Master-data conflict — do **not** pick.
  Surface both and hand back for a mapping cleanup.
- **A 3-B row has `Value` populated but no `AssetData.v_Price`** for the
  date. The proc will derive `Price` from `Value / (Quantity × ContractSize)`
  at write time — the row **is** resolvable; write it. Verify the derived
  price against a sanity range if the caller wants a sanity check
  (out-of-scope by default).
- **A row's blocker cleared but `RawTransaction` is huge (JP, MS).**
  Preserve it verbatim anyway — don't attempt to shrink it; the `RawTransaction`
  column takes `nvarchar(max)`.
- **The scope is a wide filter and returns hundreds of rows.** Confirm
  with the user before submitting the batch — atomicity means one bad
  row rolls back the lot, and rolling back hundreds of expected fixes
  because of one edge case is a UX failure. Offer to split by bucket, or
  by `(Account, Custody)`, and process each sub-batch in sequence.
- **Everything came back Skipped or Reported — no rows to write.** Say so
  explicitly and cite the reason bucket — an empty write set is a valid
  outcome (the audit was right, master data just isn't ready yet).
