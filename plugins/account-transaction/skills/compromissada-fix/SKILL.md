---
name: compromissada-fix
description: "Use when the user wants to fix / normalise COMPROMISSADA (repo) trades in Portfolio.AccountTransaction — XP feeds load repos priced in the underlying debenture's units (Quantity = unit count, Price = unitPrice) instead of cash-like (Quantity = financial value, Price = 1). This skill audits, pairs BUY↔SELL, applies the R6 fix lock-aware and pair-consistent, and reports what it couldn't touch. A repeatable batch correction, not a one-off."
---

# Normalise COMPROMISSADA (repo) trades

You are the orchestrator for normalising *compromissadas* (repos / `productType = "Repo"`) in
`Portfolio.AccountTransaction` onto Sten's **cash-like convention**: `Quantity` = the financial
value in account currency, `Price` = 1 on the BUY (and the tiny repo yield on the SELL). XP sends
these priced in the **underlying debenture's units** (`Quantity` = a small unit count, `Price` =
`unitPrice`), which has to be rewritten. **BTG already loads cash-like — verify, but expect nothing
to fix there.**

This is a **self-contained orchestration skill**. The full worked recipe is **R6** in
[`ayunit://docs/transaction/fixes`](ayunit://docs/transaction/fixes) — read it first; this skill is
the *batch* procedure (scope → pair → lock-gate → canary → batch → verify) wrapped around it.

## Inputs

A **custody** (default both `XP` and `BTG`) and optionally a **single account** to scope to. If the
user named an account, restrict every query to it. Echo the resolved scope (custody / account /
date window) at the start of every report.

## Reference resources (read on demand)

| Resource | Read when… |
|---|---|
| [`ayunit://docs/transaction/fixes`](ayunit://docs/transaction/fixes) | **First.** R6 = the exact BUY/SELL fix math, before/after, AgentCheck format. Plus the universal write guardrails (SELECT-first-merge, drop `AccountCurrency`/`AccountFx`, absolute values, AgentCheck). |
| [`ayunit://docs/checkeddate/usage`](ayunit://docs/checkeddate/usage) | **Before any write.** The lock contract: the proc rejects a write when an `Activated=1` `v_CheckedDate` exists for `(Account, Custody)` and `Date` **or** `SettlementDate` ≤ that lock date. |
| [`ayunit://docs/transaction/procedure`](ayunit://docs/transaction/procedure) · [`types`](ayunit://docs/transaction/types) | Column-level `Portfolio.AccountTransaction_Update` params and the sign table (proc applies the sign from `TransactionType` — always pass **absolute** values). |

## Tools you call directly

- `execute_select_query` — every read (audit, pairing, lock lookup, verification).
- `execute_procedure(procedure='Portfolio.AccountTransaction_Update', cmd='U', …)` — the only write path.
- `get_procedure_detail` / `get_view_detail` — confirm params/columns; never guess.

> **Large batches.** When there are dozens of pairs, it's fine to script the same two operations
> (read-only `SELECT` + `execute_procedure`) in a loop rather than hand-issuing each — but the logic
> below is unchanged, and you still canary + verify. (Off-MCP, the same calls go through the Ayunit
> REST API: `POST /api/v1/introspection/{db}/query` for reads and `…/execute-procedure` for writes,
> creds in `.env`. Identical contract.)

## The correction cycle

### 1 — Scope & classify

Run the R6 audit per custody. A repo row is **malformed** when `Price` is far from 1 (it's the
debenture `unitPrice`); **cash-like** when `Price ≈ 1` and `|Quantity| ≈ |Value|`.

```sql
SELECT Custody, Status,
       SUM(CASE WHEN Price > 1.01 OR Price < 0.99 THEN 1 ELSE 0 END)              AS malformed,
       SUM(CASE WHEN Price BETWEEN 0.99 AND 1.01 THEN 1 ELSE 0 END)              AS cashlike
FROM Portfolio.v_AccountTransaction
WHERE Custody IN ('XP','BTG')
  AND (Asset = 'COMPROMISSADA' OR AssetCustody LIKE '%COMPROMISSADA%')
  AND Status <> 'UPDATED'                       -- already-fixed rows are out of scope
GROUP BY Custody, Status;
```

Report the split. If BTG shows `malformed = 0` (the normal case) tell the user BTG is clean and
work only XP. **Only `VALIDATED` rows are realistic targets; `UPDATED` = already done.**

### 2 — Pull the universe & pair BUY↔SELL

Fetch **all** in-scope COMPROMISSADA rows (any status, so SELLs can find their BUY) including
`RawTransaction`, `Date`, `SettlementDate`, `Value`. Pairing keys live in `RawTransaction`, because
`CustodyIdentifier` is sometimes NULL:

- `clientId` + `cetipSelicCode` (= the leg) + `quantity` (raw **unit count**, identical on the
  buy and its redemption) → FIFO match each Purchase to the next Sale on/after its date.

Each matched `(BUY, SELL)` is a **tranche**; an unmatched BUY is an **open repo**; an unmatched
SELL is an **orphan** (can't be fixed — see § 6).

### 3 — Compute the fix (R6)

`Value` **never changes**; only `Quantity` / `Price` / `PriceExFee`. Pass **absolute** values.

- **BUY:** `Quantity = ABS(Value)`, `Price = 1`, `PriceExFee = 1`.
- **SELL:** `Quantity = ABS(BUY.Value)` (the principal — so the two legs match), and
  `Price = PriceExFee = ABS(SELL.Value) / Quantity` (lands just above 1 — the repo yield).

**Sanity gate:** a computed SELL price outside `0.99 … 1.05` means the pairing is wrong — do **not**
write it; route to orphan/manual review.

### 4 — Lock-gate (CheckedDate) and pair consistency

Read the active locks once: `SELECT Account, Date FROM Portfolio.v_CheckedDate WHERE Custody = …
AND Activated = 1` (flag any `(Account,Custody)` with >1 active row — the proc's scalar subquery
raises on duplicates; skip those accounts and report).

A leg is **writable** only if `Date > lock AND SettlementDate > lock` (or no active lock).

> **Never half-fix a pair.** If a lock falls *between* a tranche's BUY and SELL, fixing only the
> open leg leaves `|BUY.Qty| ≠ |SELL.Qty|` — a broken pair. So gate at the **tranche**: write its
> fixes **only if every malformed leg in it is writable**; otherwise skip the whole tranche and
> report it as lock-blocked. (Open-repo BUYs and tranches where only one leg is malformed follow
> the same rule on whichever legs need fixing.)

### 5 — Canary, then batch, then verify

1. **Canary.** Fix **one** clean tranche, re-SELECT it, show the user before→after, and pause for
   approval before the rest. (Confirm the write path works and the convention matches.)
2. **Batch.** For each fix: **SELECT-first-merge** — read the full current row, build params from
   every populated column, **drop `AccountCurrency` and `AccountFx`** (the proc computes them and
   rejects the whole payload otherwise), overlay `Quantity`/`Price`/`PriceExFee`, set
   `Status = 'UPDATED'`, and set `AgentCheck`. Write `@CMD='U'`.
   - `AgentCheck` format (per leg):
     `fix YYYY-MM-DD: XP repo BUY normalised to cash-like - Quantity <old>-><new> (=ABS(Value)), Price <old>->1, PriceExFee->1 [R6]`
     `fix YYYY-MM-DD: XP repo SELL normalised - Quantity <old>-><new> (=ABS(BUY pk <n> Value)), Price <old>-><new> (=ABS(Value)/Quantity); Value unchanged (pairs BUY <n>) [R6]`
3. **Verify sweep.** Re-SELECT every written pk. Confirm `Status = 'UPDATED'`, BUYs have
   `Price = 1` and `|Quantity| = |Value|`, SELLs have `0.99 ≤ Price ≤ 1.05`. Then re-run the § 1
   audit and confirm the only malformed rows left are the ones you deliberately skipped.

### 6 — Report what was skipped

End every run with the three buckets:

| Bucket | Meaning | Disposition |
|---|---|---|
| **Fixed** | written + verified | done |
| **Lock-blocked** | trade `Date ≤` the account's active CheckedDate | needs a CheckedDate move/deactivation — **user decision**, audited, via `Portfolio.CheckedDate_Update` (not allowlisted → emit a copy-paste `EXEC`). Don't touch locks silently. |
| **Orphan / duplicate SELLs** | SELL with no resolvable BUY principal; often exact-duplicate rows from a double load | manual review — frequently an IGNORE (`@CMD='U'`, `Status='IGNORED'`) or delete, **not** an R6 normalise. List each with its `RawTransaction`. |

## Critical rules

- **Never write a row whose `Date` or `SettlementDate` is on/before the account's active CheckedDate** — the proc rejects it, and forcing it means lifting a lock (user-approved only).
- **Never half-fix a pair** — gate at the tranche; both legs cash-like or neither.
- **`Value` never changes** — if a "fix" would change `Value`, the pairing or the leg sign is wrong; stop.
- **Always SELECT-first-merge and drop `AccountCurrency`/`AccountFx`** before the write (else generic 400 before SQL runs).
- **Always pass absolute `Quantity`/`Price`/`Value`** — the proc applies the sign from `TransactionType`.
- **Always set `AgentCheck`** with the `[R6]` tag so the next session can read the fix.
- **Canary before any batch**, and **re-audit after** to prove only intended rows remain.
- **Reply in the user's language** (PT/EN) and echo the resolved scope.

## When unsure

- **A custody you didn't expect shows malformed repos** → re-derive the unit-vs-cash signature from
  that feed's `RawTransaction` (field names differ per custody) before trusting the audit.
- **SELL price lands outside 0.99–1.05** → the buy you paired is wrong (rolled repo, same
  units different tranche). Re-pair FIFO by date or treat as orphan.
- **Duplicate-looking SELLs** (same account/cetip/units/Value, no buy) → likely a double load;
  propose IGNORE/delete, don't normalise.
- **A whole account is lock-blocked but the user wants it fixed** → explain the CheckedDate move,
  get explicit approval, emit the `CheckedDate_Update` `EXEC` block, then re-run this skill.
