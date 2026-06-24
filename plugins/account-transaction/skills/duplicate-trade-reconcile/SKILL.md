---
name: duplicate-trade-reconcile
description: "Use when the user wants to find and remove DUPLICATE trades in Portfolio.AccountTransaction by reconciling them against custody — restatements / double-loads of the same economic event (e.g. BTG's AQUISIÇÃO VIRTUAL → APLICAÇÃO fund-subscription cycle, or any feed that books the same trade more than once with a revised price). This skill scopes candidate trades by any AccountTransaction filter (accounts, custody, asset, date range, transaction type…), reconciles each cluster against the day-over-day quantity delta in Portfolio.CustodyPosition, deletes the duplicates it can prove with high confidence via Portfolio.AccountTransaction_Update @CMD='D' (lock-aware, dry-run first), and reports everything it could not prove for the user to decide. Trigger whenever the user says trades look duplicated / doubled / loaded twice / need de-duping, or asks to check trades against the custody position, for one or many accounts."
---

# Reconcile & remove duplicate trades against custody

You are the orchestrator for **de-duplicating** `Portfolio.AccountTransaction`. Custody feeds
sometimes book the **same economic event more than once** — a provisional row that is later
restated (BTG's `AQUISIÇÃO VIRTUAL` → `APLICAÇÃO` fund cycle), a feed re-sent on consecutive days
with a revised NAV/price, or a plain double load. Left alone, every `VALIDATED`/`UPDATED` copy
feeds `AccountPosition` and **double-counts** the holding; `PENDING` copies don't feed positions but
are noise in the book.

The **ground truth is the custody position**. `Portfolio.CustodyPosition` is the broker's
end-of-day holding per `(Account, Asset, Date)`. The day-over-day change in its `Quantity` is how
much the holding *actually* moved that day. So the real trades are the ones whose quantities
reconstruct that delta; any extra rows describing the same move are duplicates.

This is a **destructive, self-contained orchestration skill**: it issues `@CMD='D'` deletes. Auto-
delete **only** the duplicates custody proves with high confidence; everything else is *reported*,
never guessed. Always dry-run the batch and show the plan before committing.

## Inputs

Any filter expressible over `Portfolio.v_AccountTransaction` columns — one or **many** accounts,
`Custody`, `Asset` / `AssetCustody` / `CustodyIdentifier`, a `Date` / `SettlementDate` window,
`TransactionType`, `Status`, or explicit `pk_AccountTransactionID` list. Echo the resolved scope
(accounts / custody / asset / date window) at the start of every report.

- **Account keys are zero-padded to 9 digits** (`47067` → `'000047067'`). When the user gives a
  short number, resolve it first: `SELECT DISTINCT ClientAccount, Custody FROM
  Portfolio.v_AccountTransaction WHERE ClientAccount LIKE '%47067%'`.
- If the user passes explicit IDs, scope to exactly those; otherwise discover candidates by filter.

## Reference resources (read on demand)

| Resource | Read when… |
|---|---|
| [`ayunit://docs/checkeddate/usage`](ayunit://docs/checkeddate/usage) | **Before any delete.** The lock contract: the proc rejects a write when an `Activated=1` `v_CheckedDate` exists for `(Account, Custody)` and the trade's `Date` **or** `SettlementDate` ≤ the lock date. Two `Activated=1` rows for one `(Account,Custody)` break the proc (scalar subquery → error 512) — detect and skip. |
| [`ayunit://docs/transaction/procedure`](ayunit://docs/transaction/procedure) · [`types`](ayunit://docs/transaction/types) | `Portfolio.AccountTransaction_Update` params, CMDs (`I`/`U`/`D`), and the status lifecycle (only `VALIDATED`/`UPDATED` reach `AccountPosition`). |
| [`ayunit://docs/transaction/fixes`](ayunit://docs/transaction/fixes) | Universal write guardrails and the IGNORE-vs-delete distinction for double-loaded rows. |
| [`ayunit://docs/portfolio-creator/pipeline`](ayunit://docs/portfolio-creator/pipeline) | Why a surplus `VALIDATED` duplicate double-counts: each one is summed into `AccountPosition`. |

## Tools you call directly

- `execute_select_query` — every read (scope, custody reconciliation, lock lookup, verification).
- `execute_batch(items=[… cmd='D' …], allow_destructive=true, dry_run=…)` — the delete path.
  Atomic (all-or-nothing); run `dry_run=true` first, show the plan, then `dry_run=false`.
- `get_view_detail` / `get_procedure_detail` — confirm columns/params; never guess.

> `execute_batch` refuses `CMD='D'` unless `allow_destructive=true`. The CheckedDate lock is enforced
> **inside** `AccountTransaction_Update`, so a lock-blocked delete fails the whole atomic batch and
> rolls everything back — which is why you lock-gate yourself *before* building the batch (below),
> rather than letting the proc reject a mixed batch.

## The reconciliation cycle

### 1 — Scope the candidate trades

Pull the candidate universe with every field you'll reason about. Read from the **view**, never the
base table.

```sql
SELECT pk_AccountTransactionID, InputDate, Date, SettlementDate, ClientAccount, Custody,
       TransactionType, GeneralLedgerDescription, Asset, AssetCustody, CustodyIdentifier,
       Quantity, Price, ValueGross, Value, Status, RawTransaction
FROM Portfolio.v_AccountTransaction
WHERE ClientAccount IN (…) AND Custody = … AND Asset = …
  AND Date BETWEEN … AND …            -- or pk_AccountTransactionID IN (…)
ORDER BY ClientAccount, Asset, Date, InputDate;
```

**Cluster** the rows by `(ClientAccount, Custody, Asset, Date)` — within a cluster, rows that share
the **same `|Value|`** (and roughly the same `|Quantity|`) are *candidate duplicates of one event*.
A cluster with a single row is fine; ignore it. Distinct `|Value|`s in a cluster are usually
distinct trades — do **not** treat them as duplicates without custody proof.

### 2 — Pull the custody position and compute the actual delta

For each `(Account, Asset)` in scope, pull the holding across the window **plus the day before** it
(you need the prior close to compute the first day's delta):

```sql
SELECT Account, Date, Asset, AssetR, Quantity, Value, Price, PriceDate
FROM Portfolio.v_CustodyPosition
WHERE Account IN (…) AND (Asset = … OR AssetR LIKE '%…%')
  AND Date BETWEEN <window_start - a few days> AND <window_end>
ORDER BY Account, Date;
```

The **actual move** on day *D* is `CustodyPosition.Quantity[D] − CustodyPosition.Quantity[D-1]`.
This single number is the broker's truth for how many units the holding changed that day, net of
everything. (Note custody dates the holding by *settlement/position date*; a trade booked with
`Date` = trade-date and `SettlementDate` = D often shows up as the custody delta on D — reconcile on
the settlement landing, not only the trade date.)

> If there is **no `CustodyPosition` row** covering the window for that `(Account, Asset)`, you
> **cannot** prove duplicates from custody → classify the whole cluster as *report-only* (§4) and
> say custody is missing. Never delete without the position to stand on.

### 3 — Reconcile: which trades are real, which are duplicates

Within a cluster, find the subset of candidate trades whose **signed quantities sum to the custody
delta** for the matching landing date. That subset is **real**; the surplus rows describing the same
move are **duplicates**.

The clean, common case — *N near-identical rows, custody moved by exactly one of them*:

- The custody delta equals **one** trade's `Quantity` to the cent (and its `Price` matches the
  custody `Price`/NAV on that date). → **Keep that one; the other N−1 are duplicates.**
- Prefer keeping the row that ties to custody **exactly**. Among equally-tying rows, keep the
  **latest confirmed** one (most recent `InputDate`, `Status IN ('VALIDATED','UPDATED')`) — it's the
  final restatement; the earlier `AQUISIÇÃO VIRTUAL` / provisional rows are the superseded copies.

Worked example (the canonical BTG fund case): custody `Quantity` is flat for days then rises once, by
`+151,918.13898151`, on the settlement date. Four AccountTransaction rows in the cluster all carry
`|Value| = 202,018.37`; only the latest `APLICAÇÃO` row has `Quantity = 151,918.13898151` @ the
custody NAV. Keep it; the two `PENDING` `AQUISIÇÃO VIRTUAL` rows and the earlier superseded
`VALIDATED` `APLICAÇÃO` (booked at the pre-revision NAV) are the three duplicates.

**Confidence gate — auto-delete only when ALL hold:**

1. The kept row's quantity reconstructs the custody delta to the cent (tie-out exact, not "close").
2. Every row you're deleting is in the **same cluster** (same account/custody/asset, same `|Value|`)
   — a genuine restatement of the *same* event, not an independent trade.
3. After deletion, the surviving rows' quantities still reconcile to the custody delta (you didn't
   delete a row custody needs).

If any of these is shaky — custody missing, deltas don't tie, the cluster mixes distinct values, or
keeping *which* row is ambiguous — **do not auto-delete; report it (§4).**

### 4 — Lock-gate (CheckedDate)

Read the active locks once for the accounts in scope:

```sql
SELECT pk_CheckedDateID, Account, Custody, Date, Activated
FROM Portfolio.v_CheckedDate
WHERE Account IN (…) AND Custody = … AND Activated = 1 ORDER BY Account, Date DESC;
```

- Flag any `(Account, Custody)` with **>1** `Activated=1` row — the proc's scalar subquery raises on
  duplicates, so *every* write on that account fails. Skip those accounts; report.
- A trade is **deletable** only if `Date > lock AND SettlementDate > lock` (or no active lock).
  A duplicate sitting **on/before** the lock is **lock-blocked** — moving the lock back to delete it
  is audit-sensitive and a **user decision** (it triggers the full recoil cycle). Report it; don't
  touch the lock silently.

### 5 — Dry-run, confirm, delete, verify

1. **Present the plan.** For each cluster, show a table: every candidate row with `Date`,
   `SettlementDate`, `Status`, `Quantity`, `Price`, `|Value|`, the custody delta it's reconciled
   against, and the verdict (**KEEP** / **DELETE — duplicate** / **REPORT** / **LOCK-BLOCKED**).
2. **Dry-run the batch.** Build `items` of `{procedure:'Portfolio.AccountTransaction_Update',
   cmd:'D', params:{pk_AccountTransactionID:…}}` for the DELETE rows, and call `execute_batch` with
   `allow_destructive=true, dry_run=true`. Confirm every item validates and `failed_index` is null.
3. **Commit.** Get the user's go-ahead (unless they pre-authorised auto-delete for high-confidence),
   then re-call with `dry_run=false`. The batch is atomic — on any failure nothing commits.
4. **Verify sweep.** Re-SELECT the deleted pks (expect zero rows) and the kept pk (expect it still
   present, `VALIDATED`/`UPDATED`). Re-state that the kept quantity equals the custody delta. If the
   kept row impacts positions, mention `AccountPosition` is now single-counted.

### 6 — Report buckets

End every run with these buckets so nothing is silently dropped:

| Bucket | Meaning | Disposition |
|---|---|---|
| **Deleted** | high-confidence duplicates, removed + verified | done |
| **Kept** | the row that reconciles to custody | the surviving true trade |
| **Reported — no custody / ambiguous** | can't prove duplicate (custody missing, deltas don't tie, mixed values, unclear which to keep) | **user decision**; show the cluster + custody evidence so they can call it |
| **Lock-blocked** | a duplicate whose `Date`/`SettlementDate` ≤ the account's active CheckedDate | needs a CheckedDate move (user-approved, audited, via `Portfolio.CheckedDate_Update` — not allowlisted, emit a copy-paste `EXEC`), then re-run this skill |

## Delete vs IGNORE

Default for a proven duplicate is **delete** (`@CMD='D'`) — it has no economic reason to exist and is
pure noise. Use **IGNORE** (`@CMD='U'`, `Status='IGNORED'`, carrying every column + `AccountCurrency`
/ `AccountFx` dropped, set `AgentCheck`) instead only when the user wants to **retain an audit trail**
of the bad row rather than remove it, or when a row is entangled (e.g. referenced elsewhere). When in
doubt which the user prefers, ask — deletion is irreversible.

## Critical rules

- **Custody is the proof.** Never auto-delete a trade without a `CustodyPosition` delta that
  reconciles to the kept row to the cent. No custody → report, don't delete.
- **Never delete across a lock.** A duplicate on/before the active CheckedDate is lock-blocked; the
  proc will reject it and roll back the whole batch. Gate yourself first.
- **Cluster discipline.** Only rows sharing the same `(Account, Custody, Asset)` and the same
  `|Value|` are candidate duplicates of one event. Different values = different trades unless custody
  explicitly says otherwise.
- **Keep the one that ties to custody**, preferring the latest confirmed restatement; delete the
  provisional / superseded copies.
- **Dry-run before every commit**, and **verify after** (deleted pks gone, kept pk reconciles).
- **Deletion is irreversible** — when confidence is anything less than high, report instead.
- **Reply in the user's language** (PT/EN) and echo the resolved scope.

## When unsure

- **Custody delta doesn't equal any single candidate** → maybe several real trades net into one
  delta, or a partial fill — sum the subset that reconciles; if none does cleanly, report, don't
  delete.
- **The only row tying to custody is `PENDING`** while a `VALIDATED` copy exists → don't blindly
  delete the validated one; the feed may still be settling. Report the conflict for the user.
- **Cluster spans the lock** (some copies before, some after the CheckedDate) → delete only the
  writable duplicates *if* the kept row is itself writable and still reconciles; otherwise treat the
  whole cluster as lock-blocked.
- **User passed IDs that aren't actually duplicates** (custody confirms each is a distinct move) →
  say so plainly and delete nothing.
