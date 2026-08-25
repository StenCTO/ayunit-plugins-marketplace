# Recovery, state ledger, and resume semantics

The BTG onshore inception backlog is a long-running write operation
(50–200 accounts × ~30–120 seconds per account). A crash mid-flight —
MCP disconnect, transport timeout, laptop sleep, analyst-triggered stop
— must NOT leave the backlog in an unrecoverable state.

This file describes the state ledger contract, the resume story, the
`force` flag semantics, and the rollback recipe for the seed positions.
**The `CheckedDate` advance is NOT rollback-able via the MCP** — see §4
for details. This makes the reconcile pass criteria in SKILL.md §Step
3.7 the last safety gate before an effectively-permanent write.

## 🚨 The lock is forward-only (verified 2026-08-20)

`mcp__ayunit__execute_checked_date` is the **only** MCP write path to
`Portfolio.CheckedDate`. Its guard-rails (from the tool description):

- `cmd='I'` (create) — allowed only if NO CheckedDate row exists for
  the `(account, custody)` pair.
- `cmd='U'` (advance) — allowed only if the new `date` is **STRICTLY
  LATER** than the current locked date.
- **No deactivate.** No delete. No move-backward. Period.

`Portfolio.CheckedDate_Update` is **NOT** in the `execute_procedure`
allowlist either — by explicit design. So once this orchestrator calls
`execute_checked_date CMD='I' account=<X> custody=BTG date=<cutoff>
activated=1`, the lock is effectively permanent from the MCP's
perspective. Any rollback of a wrongly-advanced lock requires DBA
intervention outside the MCP (direct SQL via SSMS with elevated
privileges).

**Practical implication:** the SKILL's §Step 3.7 reconcile pass
criteria and the tranche-checkpoint pause are the two remaining safety
gates before an irreversible write. The pilot tranche is the highest-
leverage supervision moment in the entire backlog.

Cross-references:
- SKILL.md §State / idempotency — the storage locations.
- SKILL.md §Autonomy contract — why the safety net is compounded
  (per-account state + tranche discipline + `dry_run` contagion).
- [`step-schemas.md`](step-schemas.md) — the JSON shapes the ledger
  mirrors.

---

## 1. The state ledger — contract

Path:
`state/inception/<cutoff_date>_btg_onshore_onboarding.ledger.json`

Shape:

```json
{
  "meta": {
    "cutoff_date": "YYYY-MM-DD",
    "custody_name": "BTG",
    "started_at": "…",
    "last_updated_at": "…",
    "routine_version": "0.1.0"
  },
  "backlog": [
    "<9-digit>", "…"
  ],
  "accounts": {
    "<9-digit>": {
      "status": "pending" | "in_progress"
              | "clean_locked" | "seeded_unreconciled"
              | "excluded" | "blocked_asset_register" | "blocked_no_price"
              | "lock_failed" | "lock_conflict" | "custody_load_lag"
              | "feed_unavailable" | "asset_disambiguation_needed"
              | "seed_build_failed" | "failed",
      "seeded_pks": [0, 0, 0],
      "lock_pk": 0,
      "unblocked_pks": [0, 0],
      "tranche": 0,
      "started_at": "…",
      "finished_at": "…",
      "error": null,
      "note": null
    }
  }
}
```

**Write discipline:**

- Write the ledger after **every** account completes (any terminal
  status). Never batch: a crash between writes should lose at most one
  account's progress.
- Write is **atomic**: write to a temp file (`.ledger.json.tmp`), fsync,
  rename over the destination. Never truncate-in-place.
- Append semantics on the `accounts` map (keyed by account code); never
  clear.

**Read discipline:**

- On every run start (after the state-lock check), read the ledger if
  it exists. Its presence signals a partial run.
- Skip accounts whose ledger row is `status ∈ {clean_locked, excluded,
  blocked_asset_register, blocked_no_price, seeded_unreconciled,
  lock_failed, lock_conflict, custody_load_lag, feed_unavailable,
  asset_disambiguation_needed, seed_build_failed, failed}` — every
  terminal status is respected on resume without `force`.
- Retry `in_progress` accounts (crashed mid-flight; the pre-flight
  gates will decide whether to seed or exclude — SELECT-before-insert
  handles the "already seeded" case).
- Retry `pending` accounts (never started).

**On `force = true`:**

- The ledger is ignored for the backlog decision (every account in
  scope is re-processed from scratch).
- The state lock is bypassed.
- **The pre-flight gates still apply** — an account that was
  successfully seeded on the prior run will re-fail Gate 1b (view
  AccountPosition rows exist) and be `excluded`. `force` does NOT
  reseed.
- Use `force` only after a manual rollback (§4) or when the ledger is
  known-stale (e.g. deleted from a different machine, testing).

---

## 2. Resume story per-account

The per-account cycle in SKILL §Step 3 is idempotent by design:

- **Pre-flight** — if the account was partially seeded (some rows
  landed, some didn't), Gate 1b (`n_view > 0`) fails and the account
  is `excluded`. The analyst then decides: manual rollback → re-run
  with `force`, OR accept the partial seed as-is and hand-finish.
- **Feed pull** — idempotent; safe to re-run.
- **Asset triage** — the leaves (`asset-lookup`, `assetcustody-fill`,
  `asset-register`, `asset-price-history`) are all idempotent on the
  happy path. `assetcustody-fill` skips inserts that already exist;
  `asset-register` refuses duplicates; `asset-price-history` skips
  dates already covered.
- **Seed INSERT** — guarded by SELECT-before-insert per row (SKILL
  §Step 3.6). A re-run on a fully-seeded account is a no-op (every
  existence check hits `n = 1`, INSERTs skipped, batch reports
  `duplicates_skipped = <asset_count>`).
- **Reconcile** — idempotent (read-only).
- **CheckedDate advance** — the `execute_checked_date` `CMD='I'` call
  will fail if an active lock already exists at that
  `(Account, Custody, Date)`. Catch the error, verify via
  `v_CheckedDate`, and if a lock is confirmed present, mark
  `clean_locked` normally.

So a resume on a mid-account crash **either** completes the account
cleanly OR flags it for human-action — never leaves it in a broken
state.

---

## 3. Where a resume is NOT safe

Two failure modes require analyst intervention before resume:

### 3.1 Partial seed with view/base skew

If the seed batch crashed mid-way AND the base-table INSERT partially
committed to rows the view doesn't show (NULL-FK on `fk_AssetID` /
`fk_CustodyID`), the pre-flight `n_view` gate returns 0 (Gate 1b
passes) but `n_base` returns non-zero (Gate 1c fails). The account
correctly moves to `excluded`, but a re-run with `force` won't help —
the base-row NULL-FK issue must be resolved by hand (either fix the
FK or `AccountPosition_Update CMD='D'` per pk).

**Prevention**: SKILL §Step 3.6 mandates a re-SELECT after each INSERT.
An INSERT that lands with NULL FK will show `n_view = 0` on the
re-SELECT; the batch stops there.

### 3.2 CheckedDate advanced on a bad seed — DBA-only recovery

Should not happen (SKILL §Step 3.7 reconcile hard-gates the advance
on `d_qty = 0`), but if it does:

- The daily routine will read this account cleanly (it has an active
  `CheckedDate` at cutoff) but the underlying `AccountPosition` may
  be wrong.
- **The orchestrator cannot recover this.** MCP has no path to
  deactivate or delete a `CheckedDate` row.
- Recovery requires the analyst to escalate to a DBA to:
  1. Delete or deactivate the `CheckedDate` row directly in the base
     table (`Portfolio.CheckedDate`) via SSMS.
  2. Delete every wrong seed row: `Portfolio.AccountPosition_Update
     CMD='D' @pk_AccountPositionID = <pk>` per pk from the ledger.
  3. Confirm both are cleared.
  4. Re-run the orchestrator for that account with `force`.

Because this path is expensive (DBA involvement + audit-log entries
that survive forever), the SKILL treats the reconcile gate + the
pilot-tranche pause as the effective non-negotiables.

---

## 4. Rollback recipe

### 4.1 Positions-only rollback (MCP path — before the lock is advanced)

If the account is `status = "seeded_unreconciled"` or the analyst
catches a problem BEFORE the lock advances (the design intent), the
orchestrator can roll back the seed via the standard MCP path:

```
mcp__ayunit__execute_procedure(
    procedure='Portfolio.AccountPosition_Update',
    cmd='D',
    params={'pk_AccountPositionID': <pk>}
)
-- repeat per pk from ledger.accounts.<account>.seeded_pks
```

Then re-verify:

```sql
SELECT COUNT(*) FROM Portfolio.v_AccountPosition
WHERE Account = '<account>' AND Custody = '<custody_name>';
-- must return 0

SELECT COUNT(*) FROM Portfolio.AccountPosition
WHERE Account = '<account>';
-- must return 0 (view/base parity)
```

Edit the ledger: set the account's `status` back to `"pending"` and
clear `seeded_pks`. Re-run with `force`.

### 4.2 Post-lock rollback (DBA path — outside the MCP)

If the lock was advanced before the problem was caught, MCP has no
path. Escalate per §3.2 above.

**Positions-side note:** `Portfolio.AccountPosition_Update CMD='D'`
IS lock-guarded — attempting `D` on a row whose date is `≤` the
active `CheckedDate` will be rejected. So even the MCP position-
delete requires the DBA to first drop the lock at the base-table
level. In practice: DBA drops the lock → then the analyst can use
the MCP `CMD='D'` per pk. Two separate steps.

**Rollback of sweep-side leaf writes** (§Step 4) is per the leaf's own
rollback contract — `duplicate-trade-reconcile` IGNORE is reversible
(`Status UPDATED → PENDING/VALIDATED`); `pending-revalidate` promotion
is reversible (`Status VALIDATED → PENDING`); `pending-position-repair`
writes are reversible by the leaf's own `--rollback` mode.

---

## 5. `force` flag semantics — cheat sheet

| Situation | `force = false` | `force = true` |
|---|---|---|
| No prior state / ledger | Runs normally. | Runs normally (`force` is a no-op). |
| Ledger present, some accounts `clean_locked` | Resumes; skips locked accounts. | Re-processes every account; locked ones re-fail pre-flight and are `excluded`. |
| Ledger present, some accounts `failed` | Skips failed accounts (no auto-retry). | Retries every account. |
| Final state lock present | Short-circuits; prints prior report. | Ignores the lock; runs the backlog. |
| Prior run's `dry_run = true` | Ledger is not read (dry-run never wrote it). | Same. |

**Never use `force` in production without a rollback first if the
prior run wrote any state.** `force` is for testing and post-crash
manual recovery, not for "restart from scratch on a live backlog."

---

## 6. What the analyst sees on resume

The orchestrator's opening echo when a ledger is found:

```
Resuming BTG onshore inception backlog for cutoff <cutoff_date>.

Prior run: <ledger.meta.started_at> → <ledger.meta.last_updated_at>
Ledger status:
  ✅ clean_locked            <n>   (skipping)
  ⚠️  seeded_unreconciled    <n>   (skipping — human-action)
  🚫 excluded / blocked      <n>   (skipping)
  ❌ failed                  <n>   (skipping — force=true to retry)
  ⏳ pending                 <n>   (will process)
  🔄 in_progress             <n>   (crashed mid-flight; will retry)

Backlog remaining: <n> accounts, R$<sum>
Next tranche will start with <first 3 accounts>.
```

The analyst can then set `force = true` to override, or intervene to
clear specific `failed` / `blocked_*` accounts before the resume runs.
