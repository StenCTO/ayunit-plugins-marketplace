---
name: btg-onshore-onboarding
description: "Use when the user wants to run the **BTG onshore inception-position backlog pipeline** end-to-end — enumerate every BTG onshore ClientAccount that has non-zero rows in `Portfolio.v_CustodyPosition` on the cutoff date but no `Portfolio.CheckedDate` for (Account, BTG) yet, walk each one through the full inception cycle (pre-flight → BTG feed pull → asset triage → seed build → canary insert → seed reconcile → CheckedDate advance → post-cutoff PENDING sweep), and emit a per-tranche structured report + XLSX tracker the analyst reads instead of doing the work by hand. This is an **orchestrator skill** (meta-skill): it chains leaf skills from `asset` (asset-lookup, assetcustody-fill, asset-register, asset-price-history), `position` (inception-position invariants — the seed shape lives in `references/seed-build-conventions.md`), and `account-transaction` (pending-revalidate, pending-position-repair, duplicate-trade-reconcile) in a fixed order per account. Unlike `daily-btg-onshore-routine` (the daily reconciliation twin, which never advances CheckedDate) this skill **does** advance CheckedDate per account (`execute_checked_date` CMD='I') after seed-reconcile passes — inception IS by definition the creation of the first lock, and the pre-inception checks + custody-vs-seed reconcile are the safety net that replaces the analyst's manual lock-move review. Sibling of `daily-btg-onshore-routine` (the daily twin). Fires on prompts like 'run the BTG onshore inception', 'roda o onboarding BTG onshore', 'seed inception BTG onshore accounts on <date>', 'BTG onshore inception backlog', 'onboard BTG onshore accounts to <date>', 'cadastra as posições iniciais BTG onshore em <data>'."
---

# BTG Onshore inception-position backlog — seed, reconcile, lock, sweep

You are the orchestrator for Sten's BTG onshore **onboarding / inception**
backlog. The daily custody routines (`daily-btg-onshore-routine`) assume every
account already has an `AccountPosition` chain rolling forward from a first
frozen `CheckedDate`. This skill is what puts every account into that state:
it enumerates the BTG onshore accounts that have custody rows on a cutoff
date but no `CheckedDate` yet, seeds their inception `Portfolio.AccountPosition`,
reconciles against custody, freezes the seed, and sweeps the transactions
loaded after the cutoff. Once this skill has processed an account, the
daily routine can pick it up on the next run.

This is a **meta-skill / orchestrator** with **write autonomy** on
`Portfolio.AccountPosition` (per-row inception INSERTs following the seed
recipe in `references/seed-build-conventions.md`), on `Portfolio.CheckedDate`
(per-account `CMD='I'` after seed-reconcile passes — see Autonomy contract
below), and on every leaf skill's write surface. Every write is either (a)
delegated to a leaf skill (each leaf owns its own guardrails: lock awareness,
SELECT-first-merge, sign convention, `AgentCheck` audit trail) or (b) applied
by the orchestrator itself as a documented recipe under `references/`. Ad-hoc
writes are not allowed.

## Autonomy contract — key divergence from `daily-btg-onshore-routine`

`daily-btg-onshore-routine` **never** advances `CheckedDate` (analyst's
approval step, owned by the `checkeddate-update` specialist path). This
inception skill **does** — with two justifications:

1. **Inception is by definition the creation of the first lock.** There is
   no prior `CheckedDate` to move; there is no state to reconcile past. The
   `CheckedDate` at the cutoff date is what promotes the seeded
   `AccountPosition` rows from "provisional" to "canonical" and is what
   lets the daily routine (and the PortfolioCreator pipeline) take over
   from `D+1`.
2. **The pre-inception checks + custody-vs-seed reconcile are the safety
   net that replaces the analyst's manual lock-move review.** The daily
   routine's rationale for refusing to advance is that the analyst has to
   validate the reconciled position before committing to freeze it; here,
   the orchestrator runs the equivalent validation (0 `AccountPosition`
   rows in the view AND base table pre-seed, 0 `CheckedDate` rows
   pre-seed, per-asset quantity diff = 0 post-seed vs custody, total
   value ties to the builder), and only advances the lock on green.
   Anything the reconcile flags stops the account — the lock advances
   ONLY on clean.

The safety net is **compounded** by keeping `dry_run` contagious (every leaf
also runs dry-run when the orchestrator does), by tranche discipline (pilot
top-3 first, then ~20 per tranche with a checkpoint report the analyst
reads before the next tranche starts), and by the per-account state ledger
(§State) that lets a partially-processed tranche resume on a re-run.

> **🚨 The `CheckedDate` advance is IRREVERSIBLE via the MCP** (verified
> 2026-08-20 against the live `execute_checked_date` tool contract).
> `Portfolio.CheckedDate_Update` is **not** in the `execute_procedure`
> allowlist, and `execute_checked_date` is forward-only (no deactivate,
> no delete, no move-backward). Once this orchestrator has advanced a
> lock, rolling it back requires DBA intervention outside the MCP
> (direct SQL via SSMS with elevated privileges). See
> [`references/recovery.md`](references/recovery.md) §3.2 for the
> DBA-only recovery path.
>
> **Practical implication:** the §Step 3.7 reconcile hard-gate
> (`d_qty = 0` on every non-cash asset, no NULL sides) and the pilot-
> tranche pause are the two remaining safety gates before an
> effectively-permanent write. Both must hold. The pilot tranche is the
> highest-leverage supervision moment in the entire backlog — never
> skip it (per SKILL Critical rules below).

**The default is autonomous end-to-end execution.** The SKILL does NOT ask
per-account preview/confirm questions ("A/B/C" style). It runs each account's
full 9-step cycle — pre-flight → feed pull → asset triage → seed build →
INSERT → reconcile → **lock advance** → post-cutoff sweep — without stopping,
UNLESS a "big incoherence" gate trips (see below). Reports print to chat +
disk on tranche boundaries; interventions happen at tranche boundaries or
when a gate has already stopped an account, never as mid-account questions.

Autonomy summary per situation:

| Situation | Behaviour |
|---|---|
| Pre-flight PASSES on all 4 gates AND seed-reconcile shows 0 qty-diff on every non-cash asset AND value delta is within the "big incoherence" thresholds (below) | **Full autonomous end-to-end** — INSERT seed, verify, reconcile, advance `CheckedDate` (`execute_checked_date` CMD='I'), run post-cutoff sweep. Log every pk. No mid-account pause. |
| Pre-flight FAILS on any gate (existing AccountPosition, existing CheckedDate, unregistered ClientAccount, unexpected custody state) | **Skip the account, mark `status = "excluded"`, report as human-action.** Never seed on a failed pre-flight. |
| Seed-reconcile FAILS the **hard gate** (qty-diff ≠ 0 on any non-cash asset, OR any NULL side) | **Do not advance the lock.** Mark `status = "seeded_unreconciled"`, capture the delta, escalate as human-action. The seed row stays (the analyst can inspect + fix + retry) but the lock does not. |
| Seed-reconcile passes hard gate but trips the **"big incoherence" value threshold** (below) | **Do not advance the lock.** Mark `status = "seed_value_incoherent"`, escalate as human-action with the delta breakdown. Seed rows stay. |
| Asset triage surfaces an unregistered custody identifier that `asset-lookup` returns `NOT_FOUND` | Delegate to `asset:asset-register` (peer analogy). If it refuses (no confident peer set), mark that account `status = "blocked_asset_register"` and skip. |
| Post-cutoff PENDING sweep — a bucket the leaves handle cleanly | Invoke the leaf autonomously (`pending-revalidate`, `pending-position-repair`, `duplicate-trade-reconcile` — same recipes documented in `references/pendings-sweep-patterns.md`). |
| Post-cutoff PENDING sweep — a bucket the leaves refuse (LOW/MED confidence) OR an unknown transaction pattern | Report as human-action per the leaf's own escalation contract. |
| Any leaf raises | Capture the error, mark the account `status = "failed"`, continue with the next account. Never abort the whole run for a single account. |

### "Big incoherence" — the ONLY value-side gate that pauses autonomy

Per-asset value diffs and moderate total value diffs are **expected** and
**logged not gated** (per §Step 3.7 corrected criteria + verified on
003575819 @ 2026-06-30 where +R$14,120 / +0.25% total delta on 11 assets
was entirely price-source driven — autonomous advance was correct).

A "big incoherence" pauses the lock advance only when **both** of the
following hold on the seed-vs-custody reconcile totals:

- `|SUM(seed_value) − SUM(cust_value)| > 5% × ABS(SUM(cust_value))`, **AND**
- `|SUM(seed_value) − SUM(cust_value)| > R$10,000` (absolute floor to
  avoid tripping on tiny accounts).

Both thresholds together protect against (a) a small % on a huge account
(a wire got missed — expensive absolute delta), and (b) a large % on a
tiny account (a systematic bug — small absolute value but wrong pattern).
Rows above the threshold: `status = "seed_value_incoherent"` → escalate;
lock does NOT advance. Rows below: **advance autonomously**, log the
delta breakdown in the tranche report for the analyst's read-later
review.

Verified thresholds (as of 2026-08-20):
- 003575819 @ 2026-06-30 — 0.25%, R$14,120 → **autonomous** (verified end-to-end).
- 000215878 @ 2026-06-30 — 0.33%, R$59,609 → **autonomous** (verified end-to-end).

These thresholds may be tuned per-analyst preference — override via input
`incoherence_pct_threshold` / `incoherence_abs_threshold_brl` (defaults
`5.0` / `10000`).

The **safety net** is the state ledger + tranche discipline: even if one
account is mis-seeded, the ledger records exactly which pks landed and
which lock was advanced, so a rollback is mechanical (`AccountPosition_Update
CMD='D'` per pk + `CheckedDate_Update CMD='U' @Activated=0`). But — because
we advance the lock only on clean reconcile, the mis-seed-and-lock case
should not fire on the happy path.

## Leaf skills it invokes (per account, in order)

| # | Skill | Purpose |
|---|---|---|
| A | `asset:asset-lookup` | Read-only pre-check on every unmapped `TickerCustody` / `CustodyIdentifier` from the account's cutoff-date custody rows. Catches "already registered but the loader didn't consult v_AssetCustody" — the recurring BTG pattern (verified cases: numeric `assetCode 307807`, CDB `CDB4267Z8IU`, CNPJ `44173493000137`). |
| B | `asset:assetcustody-fill` | For every HIGH-confidence hit from (A): INSERT the missing `Portfolio.AssetCustody` row via `AssetCustody_Update CMD='I'`, then `Portfolio.CustodyPosition_Update CMD='Update_Missing_Asset'` scoped to the account+cutoff to back-fill custody rows. Returns `unblocked_pks` for later use in (F). |
| C | `asset:register-br-funds` → `asset:asset-register` | Only for identifiers that (A) resolved to `NOT_FOUND` (genuinely new asset). BR-fund CNPJs go through the ANBIMA-enriched chain; other kinds go through peer-analogy classification. If `asset-register` refuses, mark the account `status = "blocked_asset_register"` and skip. |
| D | `asset:asset-price-history` | For every held asset without a price in `AssetData.v_Price` on the cutoff date, back-fill from the leaf's 4-source priority chain (MarketData → AssetDataDB.v_Price → `v_CustodyPosition × PriceFactor`). Window `[cutoff, cutoff]` for the seed itself; the routine does NOT backfill deeper history at inception (that's a separate operation). |
| E | *(built-in recipe)* — inception seed INSERT | Per-account: build the seed rows per `references/seed-build-conventions.md`, canary the largest row via `execute_procedure Portfolio.AccountPosition_Update CMD='I'`, verify, batch the rest, verify + reconcile against `v_CustodyPosition`. This is a **documented orchestrator recipe** (per §autonomy — same discipline as `daily-btg-onshore-routine`'s pre-classifier recipes). The invariants (`Open=Close`, zero flows, `Activated=1`, defaults, sanity gates) mirror `position:inception-position` §3-§8 verbatim — but the interactive preview/confirm gate of that leaf is replaced by the pre-inception + post-seed reconcile gates codified above. |
| F | `account-transaction:pending-revalidate` | Fed the `unblocked_pks` list from (B), scoped to `SettlementDate > cutoff`, promoting PENDING → VALIDATED on the newly-mapped assets. |
| G | `account-transaction:pending-position-repair` | For PENDING rows loaded post-cutoff whose `Asset IS NULL` and cannot be resolved by identifier alone — the leaf infers `(Asset, direction)` from the AccountPosition ↔ CustodyPosition delta. HIGH-confidence only; MED/LOW comes back as human-action. |
| H | `account-transaction:duplicate-trade-reconcile` | For post-cutoff duplicate pairs (BTG re-loads, restated trades). The leaf runs the two-phase pattern (U→IGNORED → recompute+verify → D). |

Each leaf's own `SKILL.md` is **the** authority for its inputs, outputs, and
guardrails. This orchestrator must not duplicate that logic — it decides
scope, supplies evidence bundles, and reads the leaf's structured result.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `cutoff_date` | **required — no default** | The custody snapshot date to seed as the inception. Must be a real `v_CustodyPosition` snapshot date for BTG (verify via a single `execute_select_query` in pre-flight). All 6 Step-3 sub-recipes (seed build, price backfill, reconcile, lock advance) key off this date. |
| `accounts` | all BTG onshore accounts satisfying the §Step-1 audit query | Optional explicit list — pass to run a single account or a hand-picked pilot subset. Zero-padded 9-digit form (e.g. `'000047067'`), per `account-transaction:references/write-invariants.md#account-key-format`. |
| `tranche_size` | `20` | Per-tranche account count. Pilot the first tranche at `3` regardless of this value (see §Step 2). |
| `pilot_size` | `3` | Number of accounts to run in the pilot tranche before opening up to the full `tranche_size`. Set to `0` to skip the pilot (only for a re-run after a known-good pilot passed). |
| `max_accounts` | `null` (all) | Optional overall cap; useful for a first smoke test. |
| `dry_run` | `false` | `true` → every leaf skill is also dry-run / read-only, no seed INSERT, no CheckedDate advance, no pendings-sweep writes, no state lock, report under `reports/inception/dry-run/`. |
| `force` | `false` | `true` → ignore the cutoff's state lock and run anyway. Use only on a confirmed crash mid-way (§Recovery). |

**Echo the resolved `(cutoff_date, accounts scope, tranche_size, pilot_size,
max_accounts, dry_run, force)` at the start of every reply.** If `dry_run =
true`, prefix every status line with `[DRY-RUN]`. Reply in the analyst's
language (PT or EN); JSON field names always in English.

## State / idempotency

State and report locations (try in order; the first that exists wins,
otherwise create the OneDrive-synced one for a Sten analyst):

1. `%USERPROFILE%/OneDrive - STEN/Documents/sten-routines/` (preferred)
2. `%USERPROFILE%/Documents/sten-routines/` (fallback)

Subfolders (all under `inception/`):

- `state/inception/<cutoff_date>_btg_onshore_onboarding.lock` — presence
  means the full backlog was processed for this cutoff. Content: the
  absolute path of the final markdown report.
- `state/inception/<cutoff_date>_btg_onshore_onboarding.ledger.json` — the
  per-account state ledger, updated **after every account completes** (see
  §Recovery). Records `{account, status, seeded_pks, lock_pk,
  unblocked_pks, error, timestamp}` per row. Reads on re-run to skip
  already-completed accounts.
- `reports/inception/<cutoff_date>_btg_onshore_onboarding_tranche_<N>.{json,md}`
  — one report per tranche.
- `reports/inception/<cutoff_date>_btg_onshore_onboarding.{json,md}` —
  final rollup after the last tranche.

Before doing anything:

- If the final lock exists **and** `force = false`: short-circuit. Read the
  previous rollup report and reply *"BTG onshore inception backlog already
  processed for cutoff `<cutoff_date>` — here's the report"* followed by the
  **Human action required** section. **Do not** invoke any leaf skill.
- If the ledger exists (partial run) and `force = false`: **resume from the
  ledger**. Skip accounts whose ledger row is `status ∈ {clean_locked,
  excluded, blocked_asset_register, failed}` (a re-run doesn't retry a
  failed account without `force`). Continue with the untouched accounts.
- If `dry_run = true`, never read or write the state lock or the ledger;
  write reports under `reports/inception/dry-run/`.
- The lock is written only at the end of a successful full backlog run
  (§Step 9).

Per-account resume discipline is in `references/recovery.md`.

## Reference resources (read on demand)

| Resource | Read when… |
|---|---|
| [`references/seed-build-conventions.md`](references/seed-build-conventions.md) | **§Step 4 seed build — mandatory.** The codified inception-row recipe (fund Itd/accY, pension CNPJ combine, FI/COE lot matching, treasury lot merge, repo COMPROMISSADA shape, cash, sanity gates). Ported from the XP 2026-08-19 validated recipe plus BTG-specific adaptations. |
| [`references/btg-feed-shape.md`](references/btg-feed-shape.md) | **§Step 3 feed pull — mandatory on the first account.** How the `get_btg_onshore_account_information` / `process_btg_onshore_position` payload maps to the XP-equivalent categories (funds / pension / fixedIncome / treasury / stock-tradedFunds / COE / repo / cash). Dump-and-map on account 1; confirm before scaling. |
| [`references/btg-identifier-quirks.md`](references/btg-identifier-quirks.md) | **§Step 3 asset triage — mandatory.** BTG-specific mapping quirks: numeric `assetCode` as `TickerCustody`, CNPJ fallbacks, EXES numeric AssetR pattern, verified cases (307807, CDB4267Z8IU, 44173493000137). |
| [`references/tranche-and-tracker.md`](references/tranche-and-tracker.md) | **§Step 2 tranching — mandatory.** Top-down-by-value ordering, pilot-3 rule, ~20 per tranche, checkpoint report between tranches, XLSX tracker schema (Progress Summary / Done Accounts / Tranche Notes / Open Items / Backlog sheets). |
| [`references/pendings-sweep-patterns.md`](references/pendings-sweep-patterns.md) | **§Step 8 pendings sweep — mandatory when the account has post-cutoff PENDING rows.** Classification rules for post-cutoff PENDINGs — Asset-NULL pre-mapping, stale-price fund-buy verify, redemptions/UNKNOWN escalation, BTG duplicate-trade lesson. |
| [`references/step-schemas.md`](references/step-schemas.md) | **§Step 9 report — mandatory.** JSON schemas for every step's capture object + the markdown report template. |
| [`references/recovery.md`](references/recovery.md) | **§State + when a run breaks mid-flight.** State ledger contract, resume-mid-tranche story, `force` flag semantics, rollback recipe. |
| [`ayunit://docs/position/inception`](ayunit://docs/position/inception) | The full inception recipe — pre-flight gates, column-by-column defaults, EXEC template, gotchas (cross-currency, cash/FX, lending/margin subsets, bond accrued interest). **`seed-build-conventions.md` cites and extends this** for BTG-specific shapes. |
| [`ayunit://docs/position/procedures`](ayunit://docs/position/procedures) | The `AccountPosition_Update` 42-param catalog + the `I` branch's persisted-column subset. Verify before writing the seed. |
| [`ayunit://docs/checkeddate/usage`](ayunit://docs/checkeddate/usage) | The lock contract this skill advances (`CMD='I'`). Read to confirm the `Activated=1` semantics and the "no writes at `Date ≤ CheckedDate`" gate. |
| [`ayunit://docs/backoffice/daily-control`](ayunit://docs/backoffice/daily-control) | The fleet-wide reconciliation dashboard the daily sibling uses. **Not** used by this skill (which pre-dates the lock), but useful for post-inception cross-check with the daily routine. |
| [`ayunit://docs/feeds/routing`](ayunit://docs/feeds/routing) | Access-name convention for the BTG onshore feed tools (`get_btg_onshore_*`, `process_btg_onshore_*`). Required for §Step 3 feed pull and §Step 8 BTG feed cross-check. |
| [`ayunit://docs/transaction/procedure`](ayunit://docs/transaction/procedure) | The `AccountTransaction_Update` param catalog. Used by leaves during the §Step 8 pendings sweep — never called directly from this orchestrator. |

## Tools this skill calls directly

- `mcp__ayunit__execute_select_query` — every pre-flight check
  (§Step 1 audit list, §Step 3 unmapped custody count, §Step 4 hold list,
  §Step 6 reconcile), every re-verify, every ledger read/write helper.
- `mcp__ayunit__get_btg_onshore_account_information` — per-account
  holder/co-holder registration lookup. Read-only. **Does NOT return
  positions.** Signature `(access_name, account)` — no `date`. Useful
  for verifying holder identity; not the position-feed tool.
- `mcp__ayunit__process_btg_onshore_position` — **WRITE tool.** Fetches
  positions from BTG's API and **overwrites `Portfolio.CustodyPosition`
  for `(account, date)`** (deletes existing rows via `CMD='DCD'` then
  re-inserts). Historical cutoffs almost never benefit — call only when
  the cutoff is close to today AND per-lot facts (accY / originalDate /
  purchaseDate) are needed. On historical inceptions, prefer the
  already-loaded `v_CustodyPosition` snapshot and take the
  "invalid accY" branch of `references/seed-build-conventions.md` §3.1.
  See `references/btg-feed-shape.md` §1 for the full rule.
- `mcp__ayunit__process_btg_onshore_transactions_by_period` — historical
  transaction cross-check (§Step 8 fallback). Requires `access_name`.
- `mcp__ayunit__process_btg_onshore_monthly_transactions` — same as above
  scoped to the last 60 days (§Step 8 fallback). Requires `access_name`.
- `mcp__ayunit__execute_procedure` — **the two orchestrator-owned writes:**
  - `procedure='Portfolio.AccountPosition_Update' cmd='I'` — per-row seed
    INSERT (§Step 5). Following the §Step-4 recipe. Guarded by SELECT-before-
    insert (`AccountPosition` has no unique constraint on `(Date, Account,
    Asset)`).
  - `procedure='Portfolio.AccountPosition_Update' cmd='D'` — mid-batch
    rollback only (§Step 5 failure path).
- `mcp__ayunit__execute_checked_date` — per-account lock advance
  (§Step 7). `cmd='I' custody='BTG' date='<cutoff_date>'
  account='<account>'`. Verify the returned pk via `v_CheckedDate` after.
- `Skill` — invoke each leaf in Steps A/B/C/D/F/G/H (see the leaf table
  above).
- **No** direct writes to `Global.Asset`, `Portfolio.AssetCustody`,
  `AssetData.Price`, or `Portfolio.AccountTransaction` — every write to
  those tables goes through a leaf skill.

## The routine

### 1 — Pre-flight (always; refuse to proceed on any failure)

1. **MCP reachable.** `execute_select_query('SELECT 1')`. Abort on failure.
2. **BTG custody registered.** Verify `Global.Custody` has an active row
   named `'BTG'` (or whatever the caller specified for onshore — the
   authoritative name lives in `Global.Custody`, do not hardcode):
   ```sql
   SELECT Custody FROM Global.v_Custody WHERE Custody = 'BTG';
   ```
   Abort if 0 rows. Capture the exact string into `run_meta.custody_name`.
3. **Cutoff date is a real custody snapshot.**
   ```sql
   SELECT COUNT(*) AS n
   FROM Portfolio.v_CustodyPosition
   WHERE Custody = '<custody_name>' AND CAST([Date] AS date) = '<cutoff_date>';
   ```
   Abort if `n = 0` (analyst passed a date BTG never reported for).
4. **State lock / ledger.** Apply the §State rules above.
5. **`access_name` known.** Attempt a trivial dry probe of
   `mcp__ayunit__get_btg_onshore_account_information` for a known
   account. If the tool needs an `access_name` the caller didn't provide
   AND the credential-profile lookup for the BTG onshore account group is
   not already known from prior context, **ask the caller** — never guess.
   Capture into `run_meta.access_name`.
6. **Skeleton report.** Initialise the in-memory report shape from
   `references/step-schemas.md` (`run_meta.started_at = <now>`, `tranches
   = []`, per-account rows to be appended as they run).

### 2 — Step 1: enumerate the inception backlog

Build the list of BTG onshore accounts to seed. The **defining query**
(three joined conditions, per the draft):

```sql
WITH cp AS (
    SELECT Account,
           SUM(CASE WHEN Value  IS NOT NULL THEN Value  ELSE 0 END) AS total_value,
           COUNT(DISTINCT Asset) AS asset_count,
           COUNT(*)              AS row_count
    FROM Portfolio.v_CustodyPosition
    WHERE Custody = '<custody_name>'
      AND CAST([Date] AS date) = '<cutoff_date>'
    GROUP BY Account
    HAVING SUM(CASE WHEN Value IS NOT NULL THEN Value ELSE 0 END) <> 0
),
ca AS (
    SELECT ClientAccount FROM Global.v_ClientAccount
    WHERE Custody = '<custody_name>'
),
cd AS (
    SELECT DISTINCT Account FROM Portfolio.v_CheckedDate
    WHERE Custody = '<custody_name>'
),
ap AS (
    SELECT DISTINCT Account FROM Portfolio.v_AccountPosition
    WHERE Custody = '<custody_name>'
)
SELECT cp.Account, cp.total_value, cp.asset_count, cp.row_count
FROM cp
JOIN ca ON ca.ClientAccount = cp.Account   -- (b) registered under BTG
LEFT JOIN cd ON cd.Account   = cp.Account  -- (c) no CheckedDate
LEFT JOIN ap ON ap.Account   = cp.Account  -- (d) no AccountPosition (view side)
WHERE cd.Account IS NULL
  AND ap.Account IS NULL
ORDER BY cp.total_value DESC;
```

The `cp.total_value <> 0` gate drops accounts whose custody row exists but
sums to zero (already-closed accounts BTG still emits an empty snapshot for).
The `LEFT JOIN … IS NULL` on `cd` and `ap` is the "inception-shaped" gate.

**Also count the base-table AccountPosition** — the view INNER-JOINs FKs
and hides NULL-FK rows. **Filter on `Account` only** (the base table has
no `fk_ClientAccountID` / `fk_CustodyID`; see Gate 1c note below):

```sql
SELECT Account, COUNT(*) AS n_base
FROM Portfolio.AccountPosition
WHERE Account IN (<the audit list>)
GROUP BY Account
HAVING COUNT(*) > 0;
```

Any account returning here is NOT an inception (orphan NULL-FK rows on the
base table) — **remove it from the audit list** and add it to
`run_meta.excluded_accounts` with `reason = "base_table_has_rows"`. This
is the 5172216 / 2388311 XP precedent — an existing AccountPosition row
means the account was seeded before by hand and the view hides it.

Apply `max_accounts` cap after ordering. Save the final ordered list to
the ledger with every account as `status = "pending"`.

Also write the initial CSV / ledger snapshot to disk (per the draft: "save
it to CSV, and show it to me ordered by total value"). Path:
`reports/inception/<cutoff_date>_btg_onshore_onboarding_backlog.csv`.
**Show the top of the ordered list to the analyst** (top 10 rows +
"…and N more totalling R$X") before starting.

**Empty list** → jump to §Step 9 (write a "nothing to onboard" report and
finalise).

### 3 — Tranche loop

For each tranche (pilot first at `pilot_size`, then `tranche_size` per
tranche until the backlog is drained), run §Step 3.1 → §Step 3.7 for each
account in the tranche.

**Between tranches:** write a per-tranche report (`_tranche_<N>.{json,md}`)
and — unless `dry_run = true` — **pause and show the summary to the
analyst**: how many accounts were `clean_locked`, `seeded_unreconciled`,
`excluded`, `blocked_asset_register`, `failed`. On the pilot tranche this
is the go/no-go moment before opening to `tranche_size`. If the analyst
says stop, honour it; if the analyst says continue, proceed to the next
tranche. On subsequent tranches, the pause is informational — continue
autonomously unless the analyst intervenes.

Per-account state transitions per §Recovery.

#### 3.1 — Per-account pre-flight (adapted from `position:inception-position` §1)

Refuse to seed unless every gate returns clean.

**Gate 1a — ClientAccount is registered under BTG:**

```sql
SELECT ClientAccount, Client, Custody, Currency, Offshore, InputDate
FROM Global.v_ClientAccount
WHERE ClientAccount = '<account>';
```

Required: one row with `Custody = '<custody_name>'`, `Offshore = 0`.
Multi-Client mappings are a feature — grain is `(Account, Custody)`; keep
joins DISTINCT. Excluded on failure with `reason = "not_registered_btg_onshore"`.

**Gate 1b — 0 `AccountPosition` rows (view):**

```sql
SELECT COUNT(*) AS n, MIN(Date) AS first_date, MAX(Date) AS last_date
FROM Portfolio.v_AccountPosition
WHERE Account = '<account>' AND Custody = '<custody_name>';
```

Excluded on `n > 0`.

**Gate 1c — 0 `AccountPosition` rows (base table):**

The base table stores `Account` as an nvarchar column directly — there is
**no `fk_ClientAccountID` / `fk_CustodyID` FK on `Portfolio.AccountPosition`**
(verified 2026-08-20 by SQL error `Invalid column name 'fk_ClientAccountID'`
against the live schema; also documented in `position:inception-position` §8).
Filter on `Account` only:

```sql
SELECT COUNT(*) AS n_base
FROM Portfolio.AccountPosition
WHERE Account = '<account>';
```

Excluded on `n_base > 0`. A view/base skew (`n_view = 0 AND n_base > 0`)
indicates orphan NULL-FK rows — surface the count as a hard human-action
item (the analyst must inspect via the base-table pks; do NOT seed on top).

**Gate 1d — 0 `CheckedDate` rows (active OR inactive):**

```sql
SELECT pk_CheckedDateID, Account, Custody, Date, Activated, InputDate, InputUser
FROM Portfolio.v_CheckedDate
WHERE Account = '<account>' AND Custody = '<custody_name>'
ORDER BY Activated DESC, Date DESC;
```

Excluded on any row (even `Activated = 0` — investigate).

**Count of unmapped custody rows on cutoff** (informational; feeds §Step
3.3 triage):

```sql
SELECT COUNT(*) AS n_unresolved
FROM Portfolio.v_CustodyPosition
WHERE Account = '<account>' AND Custody = '<custody_name>'
  AND CAST([Date] AS date) = '<cutoff_date>' AND Asset IS NULL;
```

Not a stop; feeds §Step 3.3.

#### 3.2 — Feed pull + per-lot fact transcription (route by cutoff age)

**Route by cutoff age** (see `references/btg-feed-shape.md` §1 for the
rule):

- **Historical cutoff** (older than ~5 business days): do NOT call any
  BTG feed tool. Rely on the already-loaded `Portfolio.v_CustodyPosition`
  snapshot for `(account, cutoff)` (quantities, values, custody prices).
  Per-lot facts (`accY`, `originalDate`, `purchaseDate`) are unavailable
  → §Step 3.4 seed build takes the "invalid `accY`" branch of
  `seed-build-conventions.md` §3.1 (`AvgPrice = seed Price`, `Itd = 0`,
  `AcquisitionDate = cutoff_date`). This was the path validated end-to-
  end on account 003575819 @ 2026-06-30 (2026-08-20 run) — 11 assets,
  clean qty-reconcile.
- **Current cutoff** (today or within the current business week):
  optionally call `mcp__ayunit__process_btg_onshore_position(access_name,
  accounts=['<account>'], date='<cutoff_date>')` to obtain per-lot facts.
  ⚠️ **Note that call WRITES to `CustodyPosition`** (deletes existing
  rows for the date, re-inserts) — verify the delta against the prior
  snapshot before committing to the write, or accept the overwrite as
  intentional.

For a holder-identity cross-check (independent of position data), call
`mcp__ayunit__get_btg_onshore_account_information(access_name,
account='<account>')` — read-only; returns registration data only.

When per-lot facts are available (current-cutoff path), transcribe them
into `facts_<account>_<cutoff_date>.json`:

- Fund lot: `{cnpj, anbimaCode, quantity, price (NAV), accY, originalDate,
  irValue}`.
- Pension certificate: `{cnpj, certificateNumber, quantity, accY, originalDate}`.
- Fixed income lot: `{ticker, isin, cnpj, quantity, price, purchaseDate,
  maturity, indexer, coupon}`.
- COE lot: `{ticker, quantity, price, purchaseDate, maturity}`.
- Treasury lot: `{ticker, purchaseDate, quantity, price, secondary?}`
  (secondary vs Tesouro Direto lots of the same instrument must be
  combined per `references/seed-build-conventions.md`).
- Equity / traded fund: `{ticker, quantity, price, purchaseDate}`.
- Repo (compromissada): `{value, rate, term}`.
- Cash: `{currency, value}`.

**Transcribe EVERY fixed-income entry** — the builder's FI-match-count
gate is the safety net, not a substitute.

#### 3.3 — Asset triage

Read `references/btg-identifier-quirks.md` before this step.

For every `Asset IS NULL AND (AssetR IS NOT NULL OR IsinR IS NOT NULL OR
AnbimaCodeR IS NOT NULL)` row in the account's cutoff-date custody:

1. **Invoke `Skill('asset:asset-lookup')`** with the identifier bundle
   (raw `AssetR`, `IsinR`, `AnbimaCodeR`, any numeric `AssetR` — the BTG
   numeric assetCode). Route the leaf's verdict:
   - `FOUND` on `Global.Asset` **but not on `v_AssetCustody`** →
     `asset:assetcustody-fill` (leaf B) with the resolved `(Asset,
     Custody, TickerCustody)`. This is the recurring BTG pattern.
   - `FOUND` on `v_AssetCustody` → already mapped; the LEFT JOIN in
     `v_CustodyPosition` should have resolved it. Log as a data anomaly
     and continue (usually a stale view cache — a re-select clears it).
   - `NOT_FOUND` → hand off to `Skill('asset:register-br-funds')` if the
     identifier is a CNPJ, else `Skill('asset:asset-register')` with the
     hint bundle from the RawTransaction / GeneralLedgerDescription. If
     the register leaf refuses, mark the account
     `status = "blocked_asset_register"` and skip.

2. After the mapping chain runs, `Portfolio.CustodyPosition_Update
   CMD='Update_Missing_Asset'` (invoked inside `assetcustody-fill`)
   back-fills the account's custody rows. Re-run the Gate-1e count query
   above. Expected: `n_unresolved = 0`. If any remain, escalate — do not
   proceed to §Step 4 with an unresolved custody row.

3. For every asset the account holds on cutoff that has no
   `AssetData.v_Price` row on `<cutoff_date>`, invoke
   `Skill('asset:asset-price-history')` scoped to `[cutoff_date,
   cutoff_date]` per asset. If the leaf reports `dates_still_missing`
   for the cutoff, that asset **cannot** be seeded — mark the account
   `status = "blocked_no_price"` and skip.

#### 3.4 — Seed build

Read `references/seed-build-conventions.md` — it's the source of truth
for every column value. Summary of the grain:

- Grain: one seed row per `(Date=cutoff, Account, Asset)`.
- `Quantity` = `SUM(DISTINCT pk-aggregate) FROM v_CustodyPosition per (Account, Asset)`
  (per-pk aggregation — BTG can emit multiple rows for the same asset).
- `Price` = `AssetData.v_Price` on cutoff, TOP-1 preferring the asset's
  designated `Source` in `Global.Asset`.
- `ValueClose` = `Quantity × Price × ContractSize` (the identity check
  in §Step 3.5 is the sanity gate).
- `Open = Close`, flows = 0, `PnlExGrossUp = PnlGrossUp = PnlDaily = 0`,
  `DailyReturn = 0`, `Mtd = Ytd = 0`.
- **`ValueCloseAfterTaxes`** = `Value − provision` where `provision =
  MAX(0, custody.cval − custody.cvat)` (0 if `cvat = 0`; clamp negative
  to 0 — see sanity gate below).
- **Funds**: `Itd = accY − 1` (invalid `accY` → `Itd = 0` and `AvgPrice
  = Price`), `AvgPrice = Price / accY`, `AcquisitionDate =
  originalDate` (dummy `0001-01-01` → `AcquisitionDate = cutoff_date`).
- **Pension**: combine certificates per fund CNPJ (Quantity sum,
  Quantity-weighted `accY`, `MIN(originalDate)`).
- **Traded funds / stocks**: ticker match, `Itd = 0`, `AcquisitionDate =
  purchaseDate` else `cutoff_date`.
- **Fixed income / COE**: `Itd = 0`, `AvgPrice = Price`, `AcquisitionDate
  = purchaseDate`; match order = code → ISIN → maturity+quantity
  (same-CETIP NTN-Bs discriminate by maturity); combine secondary +
  Tesouro Direto lots of the same instrument, `AcquisitionDate =
  MIN(...)`.
- **Repo**: `TransactionType = COMPROMISSADA`; `Quantity = Value`,
  `Price = 1`.
- **Cash** (`BRL`, `USD`, etc.): `Quantity = Value`, `Price = 1`,
  `AssetGroup = Cash` shape.
- **Skip rows** with `Quantity = 0 AND Value = 0`.

#### 3.5 — Sanity gates (per row, before INSERT)

Fail the row (and stop the account with `status = "seed_build_failed"`)
on any of:

- `provision < 0` (negative provision — shouldn't happen post-clamp).
- `|ValueCloseAfterTaxes| > |ValueClose|` (net > gross).
- `AvgPrice` is `NULL` / `0` on a non-cash asset.
- `|ValueClose − Quantity × Price × ContractSize| > 1% × |ValueClose|`
  (identity check; suggests a `ContractSize` issue on the asset
  registration).
- Fixed-income match count = 0 for a held FI asset (safety net for
  §Step 3.2 fact transcription completeness).
- Sign of `Quantity` doesn't match position type (repos / CPRs
  negative; see §sign-convention in
  `account-transaction:references/write-invariants.md`).

Per-row failures accumulate in `account.seed_build_errors`; ANY failure
skips the seed INSERT.

#### 3.6 — Canary + batch INSERT

Order the account's seed rows by `|ValueClose|` DESC. **Guard every
INSERT with a SELECT-before-insert existence check** (per
`position:inception-position` §7 — `AccountPosition` has no unique
constraint on `(Date, Account, Asset)`):

```sql
SELECT COUNT(*) AS n FROM Portfolio.v_AccountPosition
WHERE Account = '<account>' AND Custody = '<custody_name>'
  AND CAST([Date] AS date) = '<cutoff_date>' AND Asset = '<asset>';
```

`n > 0` → skip (already seeded — probably a resume). `n = 0` → INSERT.

**Canary (row 1, largest by `|ValueClose|`):** one
`execute_procedure(Portfolio.AccountPosition_Update, cmd='I',
params={…minimal set per position:inception-position §6})`. Re-SELECT
to confirm exactly one row lands and `AccountFx` is sensible
(same-currency = 1.0; cross-currency ≈ cutoff rate). **Canary is a
technical safety** (catch a schema/lookup failure with one call
instead of N). It is NOT an approval checkpoint — if the canary
succeeds, proceed autonomously to the batch. If it fails, capture
the error, mark the account `status = "seed_insert_failed"`, and
STOP (no batch attempt — rollback via §Recovery §4.1).

**Batch (rows 2..N):** loop `execute_procedure` per row with the same
minimal param shape (may run in parallel — each call is a self-
contained transaction). Keep a per-row outcome ledger `{asset, pk,
status, error?}`. **Stop on the first failure**; go to §Recovery /
rollback.

#### 3.7 — Seed reconcile (Step 6 in the draft)

After the full batch is in, run the reconcile:

```sql
SELECT COALESCE(ap.Asset, cp.Asset) AS Asset,
       ap.QuantityClose AS seed_qty,  cp.Quantity AS cust_qty,
       ap.ValueClose     AS seed_value, cp.Value    AS cust_value,
       ROUND(ISNULL(ap.QuantityClose,0) - ISNULL(cp.Quantity,0), 6) AS d_qty,
       ROUND(ISNULL(ap.ValueClose,0)    - ISNULL(cp.Value,0),    2) AS d_value
FROM   (SELECT Asset, QuantityClose, ValueClose FROM Portfolio.v_AccountPosition
        WHERE Account='<account>' AND Custody='<custody_name>'
          AND CAST([Date] AS date) = '<cutoff_date>') ap
FULL OUTER JOIN
       (SELECT Asset, SUM(Quantity) AS Quantity, SUM(Value) AS Value
        FROM Portfolio.v_CustodyPosition
        WHERE Account='<account>' AND Custody='<custody_name>'
          AND CAST([Date] AS date) = '<cutoff_date>' AND Asset IS NOT NULL
        GROUP BY Asset) cp
  ON ap.Asset = cp.Asset
ORDER BY ABS(ISNULL(ap.ValueClose,0) - ISNULL(cp.Value,0)) DESC;
```

Pass criteria for autonomous lock advance (§Step 3.8) — per the XP
recipe design intent (verified 2026-08-20 on 003575819):

**Hard gates (must all pass, else the lock does NOT advance):**

- Every non-cash, non-COMPROMISSADA asset: `d_qty = 0` (to 6 decimals).
- Every asset: neither side is NULL (a NULL `seed_qty` = missed
  holding; a NULL `cust_qty` = phantom seed).

**Soft signals (logged not gated — captured in `step6_reconcile.value_diffs`
for the analyst, but do NOT block the lock):**

- Per-asset `d_value` — non-zero deltas are expected on assets where
  the seed's chosen `PriceSource` (per `Global.Asset.Source`, TOP-1 in
  `AssetData.v_Price`) differs from custody's `Source` on the same
  date. Real-world example (verified on 003575819 @ 2026-06-30): the
  seed's Anbima-preferred price for Sten Master D5 gave +R$13,763 vs
  custody's BTG-flagged price on the same date — a pricing-source
  disagreement, not a seed error.
- Total `SUM(seed_value) − SUM(cust_value)` — same rationale; a total
  gap up to a few % is expected and driven by per-asset price-source
  drift. Log the total and its % of custody total; do NOT gate on it.

**Cash asset** (`BRL` / `USD`) quantity gap alone → warn but proceed
(cash is `Value`, not a physical quantity; the value diff is captured
as a soft signal).

#### 3.8 — CheckedDate advance (autonomous on clean reconcile)

**Only** if §Step 3.7 passed:

```
mcp__ayunit__execute_checked_date(
    cmd     = 'I',
    account = '<account>',      # 9-digit zero-padded
    custody = '<custody_name>',
    date    = '<cutoff_date>',
    activated = 1
)
```

Verify:

```sql
SELECT pk_CheckedDateID, Account, Custody, Date, Activated, InputDate
FROM Portfolio.v_CheckedDate
WHERE Account = '<account>' AND Custody = '<custody_name>'
  AND CAST([Date] AS date) = '<cutoff_date>' AND Activated = 1;
```

Expected: exactly one row with `Activated = 1` at the cutoff. Capture
the pk into the ledger. Mark account `status = "clean_locked"`.

If the verify returns 0 rows or > 1 row, mark
`status = "lock_failed"`, capture the error, escalate as human-action
(seed is in, lock did not land — the analyst needs to resolve).

### 4 — Post-cutoff PENDING sweep (per account)

**Only run for accounts that reached `clean_locked` in §Step 3.8.**
Accounts still at `seeded_unreconciled` or worse are frozen at this
point — the analyst decides.

For each `clean_locked` account, invoke the sweep in this fixed order
(read `references/pendings-sweep-patterns.md` for the full recipes):

1. **`account-transaction:pending-revalidate`** scoped to `SettlementDate
   > cutoff` and prepended with the `unblocked_pks` from §Step 3.3.
   Promotes PENDING → VALIDATED where the auto-Asset-match now fires.
   For each auto-filled fund-buy price, **verify against the custody
   quantity delta before trusting it** — the stale-price lesson from
   the daily sibling.
2. **`account-transaction:pending-position-repair`** for PENDING rows
   with `Asset IS NULL` and no resolvable identifier — the leaf infers
   from the AccountPosition ↔ CustodyPosition delta. HIGH-confidence
   only; MED/LOW → human-action.
3. **`account-transaction:duplicate-trade-reconcile`** for post-cutoff
   duplicate pairs (BTG re-loads, restated trades — the AQUISIÇÃO
   VIRTUAL → APLICAÇÃO pattern; the daily-btg-onshore-routine's
   duplicate-first-pass Mode B). Two-phase U→IGNORED → recompute+verify
   → D.
4. **Redemptions and UNKNOWN cash movements** → flag as human-action;
   do NOT improvise a fix.

Capture the sweep result into `account.step4_sweep` per the schema.

### 5 — Per-tranche checkpoint report

At the end of every tranche, write `_tranche_<N>.{json,md}` and print
the summary to chat. Pause **only** on the pilot tranche (unless the
analyst says otherwise); subsequent tranches continue autonomously.

Tracker XLSX (`references/tranche-and-tracker.md` has the schema):
update the Progress Summary, Done Accounts, Tranche Notes, Open Items
and Backlog sheets after every tranche. Path
`reports/inception/BTG_Inception_Tracker_<cutoff_date>.xlsx`.

### 6 — Final rollup + state lock

After the last tranche:

- Aggregate per-tranche reports into
  `<cutoff_date>_btg_onshore_onboarding.{json,md}`.
- Aggregate counts: `accounts_total`, `accounts_clean_locked`,
  `accounts_seeded_unreconciled`, `accounts_excluded`,
  `accounts_blocked_asset_register`, `accounts_blocked_no_price`,
  `accounts_failed`, plus the leaf-level tallies from the sweep.
- Write the final markdown with the **Human action required** section
  first, then the per-tranche tables, then the per-account details.
- Write the state lock: `state/inception/<cutoff_date>_btg_onshore_onboarding.lock`
  with the absolute path of the final markdown report as content.

### 7 — Finalise

Reply in chat with:

1. One-line status summary (`OK ✅ · N clean-locked` / `⚠️ N residual`
   / `❌ N failed`).
2. The report path.
3. The tracker XLSX path.
4. The count of items in **Human action required**.
5. The resolved scope echo.

## Critical rules

- **Writes are:** (a) leaf-owned (§leaf table) — each leaf's SKILL.md
  is the authority for its own guardrails; or (b) orchestrator-owned
  under a documented recipe (§Step 3.6 seed INSERT following
  `references/seed-build-conventions.md`, §Step 3.8 CheckedDate advance
  under the pass criteria). Ad-hoc writes are not allowed.
- **Act, don't ask, on the happy path — no per-account preview/confirm.**
  Green accounts (pre-flight passes → asset triage clean → seed builds
  → reconcile hard-gate `d_qty = 0` passes → value delta within the
  "big incoherence" thresholds) run **the full 9-step cycle including
  the lock advance and pendings sweep** without a mid-account pause.
  The SKILL does NOT ask "A/B/C — advance the lock now or stop first?"
  between the reconcile and the lock advance — that step is autonomous
  when the gates hold. The pilot-tranche pause and the per-tranche
  checkpoint are the analyst's supervision layers at **tranche**
  boundaries, not per-account. A test invocation for a single account
  is treated the same way — no artificial mid-account gates.
- **Advance `CheckedDate` autonomously only on clean reconcile.** Any
  qty gap, any NULL side, any leaf refusal → the lock does not
  advance. This is the divergence from `daily-btg-onshore-routine`
  and it is safe only because the reconcile is the gate.
- **Dry-run is contagious.** If `dry_run = true`, every leaf runs
  dry-run, no seed INSERT, no CheckedDate advance, no sweep writes,
  no state lock, no ledger writes.
- **Continue-on-failure per account.** One account's failure never
  aborts the tranche. Capture in the ledger, move on.
- **Guard every seed INSERT with SELECT-before-insert.**
  `AccountPosition` has no unique constraint on `(Date, Account, Asset)`
  — a re-run silently stacks duplicates. The existence check + the
  re-SELECT after each INSERT are the invariant.
- **Scope `Update_Missing_Asset` narrowly** — the leaf
  `assetcustody-fill` already does this (triple-scoped to `(@Date,
  @Account, @Custody)`), but if you ever fall back to invoking
  `Portfolio.CustodyPosition_Update CMD='Update_Missing_Asset'`
  directly, keep the same scope. Without it the CMD back-fills
  firm-wide.
- **Never skip the pilot** except on a re-run of an already-piloted
  cutoff. The pilot is the go/no-go for the whole backlog.
- **Never overwrite the ledger silently.** Write the ledger row after
  every account finishes; use append semantics on the JSON, not
  replace. The ledger is the resume contract.
- **Reply in the analyst's language.** PT or EN. JSON field names
  always English; markdown report headings can be PT for Sten readers.
- **Echo scope on every reply.** `(cutoff_date, accounts scope,
  tranche_size, pilot_size, max_accounts, dry_run, force)` plus the
  count of accounts resolved from Step 1.

## When unsure

- **Step-1 audit returns 0 accounts.** Say so explicitly. Write a
  "nothing to onboard for BTG onshore at cutoff `<cutoff_date>`"
  report and finalise. Valid outcome — most likely the backlog has
  been drained by a prior run.
- **An account has base-table AccountPosition rows but the view is
  empty (Gate 1c fails).** Do NOT seed on top — surface the base-row
  count and pass it as human-action with `reason =
  "base_table_orphans"`. The XP 5172216 / 2388311 precedent.
- **Cutoff date has no `v_CustodyPosition` rows for BTG.** Abort with
  `reason = "cutoff_not_a_custody_snapshot"`. Do not extrapolate to
  the nearest date without asking the analyst.
- **Multiple `Activated = 1` rows in `v_CheckedDate` for the same
  `(Account, Custody)` after §Step 3.8.** Concurrent-write race (the
  analyst added a lock at the same time). Do NOT try to fix from
  here — mark account `status = "lock_conflict"`, capture both pks,
  escalate.
- **`asset-register` refuses on a candidate identifier.** Do NOT
  retry with a looser peer strategy. Log the tuple with the leaf's
  refusal and one-sentence guidance for the analyst; mark the
  account `blocked_asset_register` and continue.
- **Seed reconcile shows a systematic value diff on funds** (all
  funds off by the same %). Almost always a Price-source mismatch
  (our `Global.Asset.Source` vs BTG's `Source` in `v_CustodyPosition`).
  Report as informational; the seed is still valid — the daily
  routine will show the same offset until pricing is aligned.
- **`get_btg_onshore_account_information` returns partial or missing
  sections for an account.** Fall back to
  `process_btg_onshore_position` for the positions-only view. If
  both fail, mark the account `status = "feed_unavailable"` and
  skip.
- **A tranche shows > 30% `failed` / `seeded_unreconciled`.** Stop
  the run at the tranche boundary regardless of the analyst's
  standing autonomy. The pattern is not local — investigate before
  the next tranche. Print a hard warning; wait for the analyst.
- **The user asks to also run the daily reconciliation as part of
  this skill.** Refuse politely and point at `daily-btg-onshore-routine`.
  Once every account in the tranche is `clean_locked`, the daily
  routine picks them up on its next run — no chaining from here.
- **The user asks to backfill deep price history at inception.**
  Refuse and point at `asset:asset-price-history` (the standalone
  workflow). This skill backfills only the cutoff-day price (§Step
  3.3.3) needed for the seed; deeper history is a separate
  operation.
- **The user asks to move an existing `CheckedDate` backward for a
  seeded account.** Refuse and point at `checkeddate-update`. The
  daily routine's recoil cycle (move lock → fix trade → re-run
  PortfolioCreator → re-advance lock) is the analyst's path, not
  ours.
