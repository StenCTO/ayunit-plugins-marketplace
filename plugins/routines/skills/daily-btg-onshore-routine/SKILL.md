---
name: daily-btg-onshore-routine
description: "Use when the user wants to run the **daily BTG Onshore reconciliation routine** end-to-end — enumerate BTG onshore accounts whose CheckedDate lags behind the latest CustodyPosition snapshot, walk each one through a position reconciliation (AccountPosition ↔ CustodyPosition), route each detected defect to the appropriate leaf skill (asset-register, pending-revalidate, pending-position-repair, assetrelated-fix, duplicate-trade-reconcile, position-quantity-adjustment), re-run the PortfolioCreator via the Ayunit MCP after each fix, and produce a per-run structured report + markdown summary the analyst reads instead of doing the work by hand. This is an **orchestrator skill** (meta-skill): it does NOT write to Portfolio.AccountTransaction directly and it NEVER advances CheckedDate — every mutation goes through a leaf skill's guardrails, and CheckedDate advancement is the analyst's approval step (owned by a different specialist path, out of scope here). Sibling of daily-btg-offshore-routine (the offshore twin). Fires on prompts like 'run the daily BTG onshore routine', 'roda a rotina BTG onshore', 'reconcile onshore BTG accounts that are behind', 'daily-btg-onshore-routine for account 47067', 'audit BTG onshore late accounts and fix what you can'."
---

# Daily BTG Onshore reconciliation routine — reconcile, fix, recompute, verify

You are the orchestrator for Sten's daily BTG **Onshore** backoffice reconciliation.
Every business day an analyst manually inspects the BTG onshore accounts whose
`CheckedDate` lags behind the latest `CustodyPosition` snapshot, compares each
one's calculated position (`Portfolio.AccountPosition`) against the custody
snapshot (`Portfolio.CustodyPosition`), and — when they diverge — traces the
divergence back to a missing / wrong / stuck trade in
`Portfolio.AccountTransaction`, fixes it, re-runs the PortfolioCreator, and
re-verifies. This skill runs that whole flow autonomously and writes a report
the analyst reads instead of doing the work.

This is a **meta-skill / orchestrator** with **write autonomy on
`Portfolio.AccountTransaction`**. It invokes the leaf skills below in sequence
(each leaf owns its own write guardrails: lock awareness, SELECT-first-merge,
sign convention, `AgentCheck` audit trail), applies its own high-confidence
recipes directly (the §3.3 pre-classifier pipeline documented under `references/`), drives
the Ayunit MCP's PortfolioCreator job tool to recompute positions after each
fix, and writes a final report. It **never advances `CheckedDate`** — that is
strictly out of scope and remains the analyst's approval step.

## Autonomy contract

The orchestrator **acts** rather than **asks** whenever it has a
high-confidence recipe available. It pauses for human input only when it
genuinely cannot decide.

| Situation | Behaviour |
|---|---|
| High-confidence pre-classifier match (all seven residue signals fire) | **Write autonomously** — `Status = IGNORED`, `[PR-IGN-DEACT]` AgentCheck. |
| Leaf `pending-revalidate` — blocker provably cleared | **Invoke autonomously** (dry_run=false). Leaf's own gate decides the write. |
| Leaf `pending-position-repair` — evidence bundle supports a HIGH-confidence candidate | **Invoke autonomously**. Leaf writes only on HIGH; MED/LOW comes back as a report item. |
| Leaf `assetrelated-fix` — GL income row with resolvable AssetRelated | **Invoke autonomously**. |
| Leaf `duplicate-trade-reconcile` — clean pair against `CustodyPosition` | **Invoke autonomously**. |
| Leaf `position-quantity-adjustment` — reconciliation plug (synthetic trade) | **Do not invoke** unless the caller pre-authorised plugs at run start. Report as human-action. This is the one leaf that invents value, so it stays human-only by default. |
| Leaf returns MED/LOW confidence or refuses | **Report as human-action** — the leaf already made the safe call. |
| No matching recipe (unclassified `PENDING`, ambiguous divergence, unregistered custody identifier) | **Report as human-action** with the SQL to investigate. |
| Pricing-only residual (`QtyMismatchAssets = 0`, `PctDiffPosition` material) | **Report as informational** — pricing team hand-off. Not a routine failure. |

The **safety net** is `CheckedDate`: because this skill never advances it,
every write it applies is provisional — the analyst reviews the reconciled
position and approves it by moving `CheckedDate` forward via the specialist
path (`checkeddate-update` / `execute_checked_date`). If a leaf or pre-
classifier writes a wrong fix, the row still sits *after* the active lock and
can be corrected before approval. Autonomy without the CheckedDate advance is
recoverable by design.

## Leaf skills it invokes (in order per account)

| # | Skill | Purpose |
|---|---|---|
| A | `account-transaction:pending-revalidate` | Re-fire the loader validators on `PENDING` rows whose 3-A / 3-B / 3-C blocker (missing Asset mapping, missing Price, derivable Quantity) has now cleared. |
| B | `account-transaction:assetrelated-fix` | Fill `AssetRelated` on GL income rows (`INTEREST / DIVIDEND`) so they promote out of `PENDING`. Layouts A/B (XP), C (JP ISIN/CUSIP), **D (BTG `RENDIMENTO - <FUND> - <BOOK>`, added 2026-07-15)**. |
| C | `account-transaction:pending-position-repair` | Repair a `PENDING` row that has no valid custody identifier by inferring `(Asset, direction)` from the `AccountPosition ↔ CustodyPosition` delta on the same account/date. Custody-agnostic. |
| D | `account-transaction:duplicate-trade-reconcile` | Detect and (lock-aware) remove restated / double-loaded trades against `Portfolio.v_CustodyPosition`. |
| E | `account-transaction:position-quantity-adjustment` | For unexplained quantity deltas that reconcile against no upstream trade, write a reconciliation plug (BUY/SELL for non-cash, GL RECEIPT/DELIVERY for cash — never DEPOSIT/WITHDRAW, never ASSET RECEIPT/DELIVERY). Human-confirmed. |
| F | `asset:asset-lookup` | **Read-only pre-check** (added 2026-07-15). Before any `asset-register` hand-off, resolve the unknown custody identifier through `identify_asset` (Global.Asset direct match on ISIN/CUSIP/CNPJ/ANBIMA/…), then `Portfolio.v_AssetCustody` (per-custody `TickerCustody`/`TickerCustody2` mapping). Verified real BTG cases (NTN-B `assetCode 307807`, CDB `CDB4267Z8IU`, EXES `Cnpj 44173493000137`) resolve HERE without needing new registration — the routine skips `asset-register` and instead flags the loader-mapping gap. |
| G | `asset:asset-register` | (Referenced, not auto-invoked.) When Step 3 surfaces an unregistered custody identifier **AND `asset-lookup` returned `NOT_FOUND`**, hand off to this skill; the routine will pick the affected `PENDING` pks up on the next re-run. Never call `asset-register` without `asset-lookup` first — verified 2026-07-15 that 3 of 4 candidate "unregistered" assets on this run were in fact already registered. |

Each leaf's own `SKILL.md` is **the** authority for its inputs, outputs, and
guardrails. This orchestrator must not duplicate that logic — it decides scope,
supplies evidence bundles, and reads the leaf's structured result.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `accounts` | all BTG onshore accounts returned by the audit query (§Step 1), ordered by `PendingTrades DESC, UnmatchedAssets DESC, Account` | Narrow to one account or an explicit list for testing. |
| `dry_run` | `false` | `true` → every leaf skill is also dry-run / read-only; PortfolioCreator is not triggered; no state lock; report written under `dry-run/`. |
| `force` | `false` | `true` → ignore the day's state lock and run anyway. Use only on a confirmed crash mid-way. |
| `max_accounts` | `null` (all) | Optional cap; useful for a first-run smoke test. |

**Echo the resolved `(accounts, dry_run, force, max_accounts)` at the start of
every reply.** If `dry_run = true`, prefix every status line with `[DRY-RUN]`.
Reply in the analyst's language (PT or EN); JSON field names always in English.

## State / idempotency

State and report locations (try in order; the first that exists wins,
otherwise create the OneDrive-synced one for a Sten analyst):

1. `%USERPROFILE%/OneDrive - STEN/Documents/sten-routines/` (preferred)
2. `%USERPROFILE%/Documents/sten-routines/` (fallback)

Subfolders: `state/`, `reports/`. Files:

- `state/<YYYY-MM-DD>_daily_btg_onshore.lock` — presence means the routine
  completed today. Content: the absolute path of the markdown report.
- `reports/<YYYY-MM-DD>_daily_btg_onshore.json` and `.md` — the run's report.

Before doing anything:

- If today's lock exists **and** `force = false`: short-circuit. Read the
  previous report and reply *"Already ran today for BTG onshore — here's the
  report"* followed by the **Human action required** section from that report.
  **Do not** invoke any leaf skill.
- If `dry_run = true`, never read or write the state lock; write reports under
  `reports/dry-run/`.
- The lock is written only at the end of a successful real run (§7).

## Reference resources (read on demand)

| Resource | Read when… |
|---|---|
| [`ayunit://docs/backoffice/daily-control`](ayunit://docs/backoffice/daily-control) | **First — this is what Step 1 uses.** Fleet-wide reconciliation dashboard: request/response shape of `get_daily_control`, the six headline signals (`UnmatchedAssets`, `PctDiffPosition`, `PriceIssueAssets`, `PendingTrades`, `QtyMismatchAssets`, `AP_TotalPosition`) as of each account's `LastCustodyPositionDate`, and how each maps to the E1–E7 catalogue. |
| [`ayunit://docs/position/reconciliation`](ayunit://docs/position/reconciliation) | The 3-way `(Date, Account, Asset)` comparison + the divergence-cause table (untranslated `AssetR`, missing trade, wrong-sign, pricing divergence, lending/margin, stale price, source-vs-custody, tax) that drives Step 3 routing. |
| [`ayunit://docs/position/procedures`](ayunit://docs/position/procedures) | `CustodyPosition_Update` CMD dispatch (`SCAcc` is manual-only — this skill uses `execute_select_query` on `v_CustodyPosition` + `v_AccountPosition` instead). |
| [`ayunit://docs/portfolio-creator/pipeline`](ayunit://docs/portfolio-creator/pipeline) | Why `Status IN (VALIDATED, UPDATED)` reaches `AccountPosition` and `PENDING` doesn't — the shape of the recompute the PortfolioCreator does after each fix. §6.9 in particular: ASSET RECEIPT/DELIVERY do not generate the Step-2.5 cash side (used by the TROCA DE NOME recipe). |
| [`ayunit://docs/checkeddate/usage`](ayunit://docs/checkeddate/usage) | The lock this skill respects and never advances. |
| [`ayunit://docs/transaction/fixes`](ayunit://docs/transaction/fixes) | Recipe library — leaves reference this; the orchestrator does not. |
| [`ayunit://docs/backoffice/decision-tree`](ayunit://docs/backoffice/decision-tree) | E1–E7 error catalogue that the Daily Control signals point into. |
| [`ayunit://docs/feeds/routing`](ayunit://docs/feeds/routing) | Access-name convention for the BTG feed tools used by the cash-gap investigation Step D. |
| [`references/duplicate-first-pass.md`](references/duplicate-first-pass.md) | **Local recipe — MUST run first in Step 3.** Detects PENDING rows that duplicate a canonical VALIDATED/UPDATED sibling on `(Account, Date, Asset via AssetRelated cross-match, ABS Value)` and IGNOREs the PENDING one. Ships the `[DUP-REVERT]` tag. |
| [`references/cash-gap-investigation.md`](references/cash-gap-investigation.md) | **Local recipe — invoked from Step 2.5.** Day-by-day BRL delta walk to find the shock date, per-row classification against the recipe library, BTG feed cross-check as last resort. |
| [`references/troca-de-nome.md`](references/troca-de-nome.md) | **Local recipe.** FII name-change swap misclassified by the loader as BUY/SELL. Reclassifies to ASSET RECEIPT/DELIVERY, zeros Value/ValueGross. Ships the `[TROCA-NOME]` tag. |
| [`references/deposito-tributos-provisionados.md`](references/deposito-tributos-provisionados.md) | **Local recipe.** Tax-provisioning artifact on active funds (structurally similar to come-cotas but empirically causes no custody-side reduction). IGNORE. Ships the `[PR-IGN-TAXPROV]` tag. |
| [`references/come-cotas.md`](references/come-cotas.md) | **Local recipe** (migrated from project `CLAUDE.md §6`). Come-cotas SELL on active funds — PENDING promoted to UPDATED with fields preserved. Ships the `[CC]` tag. |
| [`references/deactivated-fund-residue.md`](references/deactivated-fund-residue.md) | **Local recipe.** Pattern + detection + fix for `PENDING` rows whose Asset resolves to a `Global.Asset.Activated = FALSE` fund with no account history. IGNORE. Ships the `[PR-IGN-DEACT]` tag. |

## Tools this skill calls directly

- `mcp__ayunit__get_daily_control` — **the Step 1 audit tool**. Fleet-wide
  reconciliation dashboard, read-only. One row per `(Account, Custody)` with
  every headline signal measured on each account's `LastCustodyPositionDate`.
  Filters are optional and AND-combined: `clients`, `accounts`, `custody`,
  `offshore`, `max_rows`. Returns `UnmatchedAssets`, `PctDiffPosition`,
  `PriceIssueAssets`, `PendingTrades`, `QtyMismatchAssets`, `AP_TotalPosition`.
  This replaces the ~90-line CTE audit query that lived here in v0.2.0 —
  same numbers, better shape, plus the new `QtyMismatchAssets` signal.
  See [`ayunit://docs/backoffice/daily-control`](ayunit://docs/backoffice/daily-control).
- `execute_select_query` — the per-account `AccountPosition ↔ CustodyPosition`
  diff (§Step 2), the re-verify (§Step 5), and the DB fallback verification
  in §3.4.
- `mcp__ayunit__portfolio_creator_health` — pre-flight service check
  (§Step 1.2). Read-only; returns service `status`/`version` plus
  `jobs_in_store` / `running_jobs` counters.
- `mcp__ayunit__calculate_portfolio` — trigger the recompute (§Step 3.3).
  **BLOCKING**: the call holds the response for the whole run (typically 10–80s,
  up to a few minutes for long histories), then returns a `job_id` with
  `status = "pending"` **even though the run already finished** (per the
  pipeline doc). The recalc is **idempotent** — safe to re-run after a
  transport timeout. Params: `end_date` (YYYY-MM-DD), `client_accounts` (list,
  passed **exactly as stored** in `Global.v_ClientAccount.ClientAccount` — for
  BTG onshore this is the 9-digit zero-padded form per CLAUDE.md §1),
  `create_after_checked_date=true` (default; the pipeline deletes and rebuilds
  `AccountPosition` / `Share` rows *after* the active lock, which is exactly
  what we want post-fix), `run_validation=false`, `consider_cpr=false`.
- `mcp__ayunit__get_portfolio_job(job_id)` — poll the returned job for
  `completed` / `failed`. Best-effort: the remote job store is in-memory
  **per worker**, so the lookup can 404 on the wrong worker even for a real
  job. When that happens, fall back to the DB check (§Step 3.3).
- `mcp__ayunit__list_portfolio_jobs` — optional recovery tool if we lose a
  `job_id`. Also per-worker-partial, so read the DB for the authoritative
  answer.
- **No** `execute_procedure`, **no** `execute_batch(dry_run=false)` from the
  orchestrator. Every `AccountTransaction` mutation goes through a leaf skill.

## The routine

### 1 — Pre-flight (always; refuse to proceed on any failure)

1. **MCP reachable.** Run a trivial `execute_select_query` (`SELECT 1`). If it
   fails, abort — do not call any leaf skill without the MCP connected.
2. **PortfolioCreator service healthy** (only when `dry_run = false`). Call
   `mcp__ayunit__portfolio_creator_health`. Abort if the service reports
   anything other than a healthy status, or if the tool itself is not
   available in the current MCP surface (e.g. the analyst's MCP wasn't
   restarted after the tool was added). Message:
   *"PortfolioCreator service not reachable / unhealthy — cannot run a real
   reconcile pass. Re-run with `dry_run = true` or fix the service first."*
   Capture the returned `version` into `run_meta` for the report.
3. **State lock.** Apply the §state rules above.
4. **Skeleton report.** Initialise the in-memory report shape from
   `references/step-schemas.md` (`run_meta.started_at = <now>`, `accounts =
   []`).

### 2 — Step 1: enumerate late BTG onshore accounts

Call the fleet-wide audit tool scoped to BTG onshore:

```
mcp__ayunit__get_daily_control(
    custody  = ["BTG"],
    offshore = false,
    accounts = <caller's accounts filter, or omit for all>,
    max_rows = 5000
)
```

The response's `data[]` has one row per `(Account, Custody)` with every
headline reconciliation signal measured **as of that account's
`LastCustodyPositionDate`**. Columns (see the Daily Control doc for the
authoritative definition):

| Column | Meaning | Healthy |
|---|---|---|
| `LastCheckedDate` | last locked (frozen) date for the pair | — |
| `LastCustodyPositionDate` | last custody snapshot date — **metrics are AS OF this date** | — |
| `LastAccountPositionDate` | last book (`AccountPosition`) date | — |
| `UnmatchedAssets` | custody rows with no resolved Asset (→ E1) | `0` |
| `PctDiffPosition` | % diff book vs custody total value | `~0` |
| `PriceIssueAssets` | assets missing price source / zero-priced on held qty (→ E7) | `0` |
| `PendingTrades` | `PENDING` transactions settling after `LastCheckedDate` | `0` |
| `QtyMismatchAssets` | assets whose book vs custody qty differ by `> 0.5` (→ E1–E5) | `0` |
| `AP_TotalPosition` | total book position value | — |

Client-side filtering:

1. **Late-account filter** — keep only rows where
   `LastCustodyPositionDate > LastCheckedDate`. An account is "late" only when
   custody has newer data than our lock; equal dates mean the analyst just
   advanced the lock (benign, not our target).
2. **Order** — sort `PendingTrades DESC, QtyMismatchAssets DESC,
   UnmatchedAssets DESC, Account ASC` so the most actionable accounts come
   first.
3. **Cap** — apply the caller's `max_accounts` cap (after ordering).
4. **Response envelope** — if `response.truncated == true`, add a warning to
   `run_meta.errors` (fleet-wide hit the cap; caller should raise `max_rows`
   or narrow `accounts`). Do **not** silently accept a truncated audit.

Capture the filtered list into `run_meta.accounts` and log the count.
`audit_summary.late_accounts_returned` = rows from the tool;
`audit_summary.late_accounts_after_filter` = rows after the late-account +
`max_accounts` filters.

**Empty list** → jump to §7 (write a "nothing to do" report and finalise).

### 3 — Per-account loop

For each late account, run the steps below in order. **Continue-on-failure**:
if any step for one account raises, capture the error, mark the account
`status = "failed"`, and move on to the next. Never abort the whole run.

Each account produces one `AccountRun` object matching the schema in
`references/step-schemas.md`. The loop appends to `run_meta.accounts`.

#### 3.1 — Step 2: position diff (AccountPosition ↔ CustodyPosition)

On the account's `LastCustodyPositionDate`, full-outer-join the two views on
`Asset`. This is the SCAcc-equivalent the analyst does by hand — the doc
`position/reconciliation` explicitly recommends `execute_select_query` on the
two views over calling `SCAcc` (which is not allowlisted).

Read both sides (two `execute_select_query` calls) and align them in-memory:

```sql
-- Calculated (book)
SELECT Asset, QuantityClose AS calc_quantity, ValueClose AS calc_value,
       ValueCloseAfterTaxes AS calc_value_taxes, PriceClose AS calc_price,
       PriceSourceClose, PriceDateClose
FROM Portfolio.v_AccountPosition
WHERE Account = '<account>' AND Custody = 'BTG'
  AND CAST([Date] AS date) = '<LastCustodyPositionDate>';

-- Reported (custody)
SELECT Asset, AssetR, Quantity AS cust_quantity, Value AS cust_value,
       ValueAfterTaxes AS cust_value_taxes, Price AS cust_price,
       Source, PriceDate, Obs
FROM Portfolio.v_CustodyPosition
WHERE Account = '<account>' AND Custody = 'BTG'
  AND CAST([Date] AS date) = '<LastCustodyPositionDate>';
```

For each `Asset` on either side, compute `(dQty, dValue, dValueTaxes)`.
Classify divergences using the table in `position/reconciliation`:

| Signal | Cause | Route |
|---|---|---|
| `custody Asset IS NULL` (untranslated `AssetR`) | Missing `Portfolio.AssetCustody` mapping | **Hand-off** to `asset-register` — flag in report; the routine can't auto-map. |
| `cust_quantity > calc_quantity` (custody has, we don't) | Missing / stuck trade | Route to `pending-revalidate` if a matching `PENDING` exists with cleared blocker; else to `pending-position-repair` if a matching `PENDING` exists with no identifier; else flag for `position-quantity-adjustment` (human-confirmed plug). |
| `cust_quantity < calc_quantity` (we have, custody doesn't) | Missing outbound trade (SELL / ASSET DELIVERY) | Same routing as above with inverted direction. This is the **step-4 scenario the analyst described** (asset disappeared from custody + PENDING with no identifier = SELL). |
| `\|dQty\| = 2 × cust_quantity` (approx) | Wrong-sign trade | Flag for human review; route to `duplicate-trade-reconcile` if a mirror row exists on the same date; else escalate. |
| `dQty ≈ 0` but `dValue ≠ 0`, `PriceDateClose < Date` | Stale price | Report only — pricing team hand-off, no leaf action from this routine. |
| Quantity matches, `dValueTaxes` differs | Tax divergence (`SellIncomeTaxes`) | Report only — analyst decision. |
| Same `(Date, Account, AssetR)` appears >1× in custody | Multiple feeds | Report only — check `Global.ClientAccount.OfficialFeed`. |

Capture as `step2_position_diff` per the schema:
`{lines: [{asset, dQty, dValue, cause, proposed_route, evidence}, …]}`.

If **no divergence** and **no residual PENDING** for the account, mark
`status = "clean"` and skip to §3.5 (verify — a no-op but records green).

#### 3.2 — Step 2.5: cash-gap investigation (MANDATORY if any cash asset diverges)

Cash divergences on `BRL` / `USD` / other cash assets are **diagnostic
signals, not residuals**. Every non-trivial cash gap points at exactly one
of: missing trade, wrong trade (misclassified), or duplicate cash-side leg.
Reporting a large cash delta as "informational" is almost always a routing
error.

**Trigger** — for each cash asset (`Asset IN ('BRL','USD',…)`) in the Step 2
diff:

- `|d_value| > 100`, **or**
- `custody Value IS NULL and book Value <> 0` (custody may not report cash
  for this account type — still investigate once), **or**
- `|d_qty| > 0.5` on `Asset='BRL'` (equivalent since BRL is Price=1).

If any triggers fire, run the four-step drill from
[`references/cash-gap-investigation.md`](references/cash-gap-investigation.md):

1. **Step A — Locate the shock date.** Day-by-day walk of book vs custody
   cash between `LastCheckedDate` and `LastCustodyPositionDate`. Identify
   the first date where the delta shifts materially.
2. **Step B — Enumerate the day's transactions.** All `AccountTransaction`
   rows on the shock date (`Date` or `SettlementDate`). Sum their `Value`.
   If the sum ≈ the observed delta jump, one or more of those rows is the
   culprit.
3. **Step C — Classify the offending row(s)** against the local recipe
   library (see the table in `cash-gap-investigation.md`). Apply the
   matching recipe autonomously; escalate anything unrecognised.
4. **Step D — BTG feed cross-check** *(last resort)*. If Step A–C don't
   converge, call `mcp__ayunit__process_btg_onshore_monthly_transactions`
   (shock date within last 60 days) or
   `mcp__ayunit__process_btg_onshore_transactions_by_period` (older). Both
   require an `access_name` credential-profile name from
   `AgnesCredentialsDB` — do not guess if not already known; ask the caller.

Any fixes applied here **populate `step3_recipes`** (below) — the
cash-gap-investigation is a routing pass, not a separate write path. Each
matched row goes through the corresponding recipe (`troca-de-nome`,
`duplicate-first-pass`, `deposito-tributos-provisionados`, `come-cotas`).

#### 3.3 — Step 3: pre-classifier pipeline + leaf routing

**All account-scoped PENDING rows AND all Step-2 shock-date rows** go
through the pre-classifier pipeline **first**, in this fixed order. Each
row is claimed by the first matching recipe and removed from later
consideration; if no recipe matches, it falls through to the leaf routing
at the bottom.

Pre-classifier order (fixed — do not reorder):

1. **`duplicate-first-pass`** ([recipe](references/duplicate-first-pass.md))
   — MUST run first on every candidate PENDING. Detects a PENDING that
   duplicates a canonical VALIDATED/UPDATED sibling on
   `(Account, Custody, Date, TransactionType, Asset via AssetRelated
   cross-match, ABS Value)`. If matched, IGNORE the PENDING (canonical
   survives). Tag `[DUP-REVERT]`. **This ordering is non-negotiable** —
   promoting a duplicated PENDING via any leaf lands double-counts in
   position (learned via failure 2026-07-15 on account 005132370).
2. **`troca-de-nome`** ([recipe](references/troca-de-nome.md)) — FII
   name-change swap cluster misclassified by the loader as BUY/SELL at peg
   prices. Reclassify to ASSET RECEIPT/DELIVERY, zero Value/ValueGross.
   Tag `[TROCA-NOME]`.
3. **`deactivated-fund-residue`** ([recipe](references/deactivated-fund-residue.md))
   — 7-signal detection on PENDING rows where the resolved Asset is
   `Global.v_Asset.Activated = FALSE` and the account has no history in it.
   IGNORE. Tag `[PR-IGN-DEACT]`.
4. **`deposito-tributos-provisionados`** ([recipe](references/deposito-tributos-provisionados.md))
   — tax-provisioning artifact on an **active** fund (structurally similar
   to come-cotas but with a distinct description and no custody-side
   reduction). Requires the empirical no-reduction check to fire. IGNORE.
   Tag `[PR-IGN-TAXPROV]`.
5. **`come-cotas`** ([recipe](references/come-cotas.md)) — SELL with
   `%COME COTAS%` description, `Value=0`, `ValueGross=IR`, identity
   `ValueGross = |Quantity| × PriceExFee` holds, asset active. Promote to
   UPDATED with fields preserved. Tag `[CC]`.

Then, for each **remaining** PENDING and each divergence line whose
`proposed_route` maps to a leaf skill, invoke the leaf **scoped to that
account + date window**, in this order:

- **A — `pending-revalidate`** — Bucket 3-A / 3-B / 3-C blockers cleared.
- **B — `assetrelated-fix`** — GL income rows still PENDING after (A).
- **C — `pending-position-repair`** — PENDING rows still stuck and
  accompanied by a matching divergence line. Pass the leaf a structured
  evidence bundle `{pk, dQty, dValue, candidate_asset, direction,
  custody_row, book_row}`. HIGH-confidence only.
- **D — `duplicate-trade-reconcile`** — when Step 2 flagged a wrong-sign /
  restatement pattern **AND** neither row is PENDING (that case belongs to
  `duplicate-first-pass` above).
- **E — `position-quantity-adjustment`** — residual delta after A–D
  reconciles against no upstream trade AND caller has pre-authorised plugs
  (default: skip; report as human-action).

Capture each recipe application into `step3_recipes.<recipe-name>` and each
leaf invocation into `step3_leaves.<leaf-name>` per the schema.

**Confidence enrichment from Step 1 audit signals.** The account's Step-1
`audit_metrics` are a strong prior for what Step 3 should find:

- `QtyMismatchAssets > 0` → an actual asset-quantity mismatch exists (not just
  pricing noise). This raises confidence in routes **C** (`pending-position-
  repair`) and **D** (`duplicate-trade-reconcile`). Cross-check: the count of
  Step-2 divergence lines with `|dQty| > 0.5` should approximate
  `QtyMismatchAssets`; if it doesn't, log a warning (Step 1 and Step 2 disagree
  on the same account/date — usually a stale custody snapshot).
- `QtyMismatchAssets = 0` **and** `PctDiffPosition` material → the entire
  delta is pricing-side. Route to **report-only**; do not invoke C/D/E even
  if a residual PENDING exists (the PENDING is unrelated to the position gap).
- `UnmatchedAssets > 0` → hand off to `asset-register` before invoking any
  leaf that depends on `Asset` resolution (routes A/C).
- `PriceIssueAssets > 0` → do **not** attempt leaf writes on those assets;
  pricing-team hand-off first.

Capture each leaf invocation's structured result into
`step3_leaves.<leaf-name>` per the schema.

**Every leaf runs with the caller's `dry_run` flag.** Dry-run is contagious.

#### 3.4 — Step 4: recompute PortfolioCreator + verify job outcome

If any pre-classifier recipe or leaf in §3.3 wrote (i.e. real run and at
least one row was updated / inserted / deleted / IGNORED), trigger the
recompute:

```
mcp__ayunit__calculate_portfolio(
    end_date = "<LastCustodyPositionDate>",
    client_accounts = ["<account exactly as stored in Global.v_ClientAccount.ClientAccount>"],
    create_after_checked_date = true,   # default; rebuilds only past-lock rows
    run_validation = false,
    consider_cpr = false
)
```

Notes on the call:

- **Blocking.** The tool holds the response for the entire pipeline run
  (~10–80s typical, up to a few minutes). The MCP wrapper waits `520s`; the
  backend times out at `500s`. Do not chunk / cancel — the recalc is
  idempotent, so if the transport times out just retry once (see below).
- **Returned `status = "pending"` is expected**, even when the run has already
  finished — the pipeline doc calls this out explicitly. Do not treat `pending`
  as a failure; treat it as "no answer yet — go verify".
- **Account format is critical.** Pass exactly what
  `Global.v_ClientAccount.ClientAccount` contains for this account. For BTG
  onshore this is the 9-digit zero-padded form (`'000047067'`, not
  `'47067'`) per `CLAUDE.md §1`. Passing the wrong form silently returns
  an empty result set — the run "succeeds" but touches no rows.
- **`from_date` is implicit.** `create_after_checked_date=true` means the
  pipeline itself decides — it walks from `active CheckedDate + 1` to
  `end_date`. This skill does **not** need to compute `from_date`.

Verify the outcome in this order (stop at the first authoritative answer):

1. **Job-status poll** — call `mcp__ayunit__get_portfolio_job(job_id)` once
   (up to 3 retries with 2s / 5s / 10s backoff on `404`, since the per-worker
   store can miss). If it returns `completed` with
   `result = {load_base: true, positions: true, shares: true}` — success,
   capture and move on.
2. **DB truth check** (fallback and belt-and-suspenders — always run this even
   when the job poll succeeded):
   ```sql
   SELECT MAX(CAST([Date] AS date)) AS MaxAccountPositionDate,
          COUNT(*)                  AS RowsInWindow
   FROM Portfolio.v_AccountPosition
   WHERE Account = '<account>' AND Custody = 'BTG'
     AND CAST([Date] AS date) BETWEEN
         DATEADD(day, 1, '<LastCheckedDate>') AND '<LastCustodyPositionDate>';
   ```
   `MaxAccountPositionDate = LastCustodyPositionDate` and `RowsInWindow > 0`
   → the recalc landed. Otherwise it did not, regardless of what the job said.

Capture the merged outcome into `step4_portfolio_creator` per the schema:
`job_id`, `job_status` (from poll — may be `null` if all polls 404'd),
`verify_source` (`"job"` | `"db"` | `"both"`), `db_max_position_date`,
`db_rows_in_window`, `started_at`, `finished_at`, `error`.

**Failure policy.**

- If the transport itself timed out (no `job_id` returned), **retry the
  `calculate_portfolio` call once** (idempotent). If the retry also times
  out, mark the account `status = "failed"`, skip §3.4, continue.
- If the DB truth check shows the recalc did not land (rows missing / stale
  max date), mark the account `status = "failed"`, capture the details,
  skip §3.4, continue.
- If the job poll says `failed`, mark the account `status = "failed"` and
  copy the job's `error` field into the account's `errors` list. Skip §3.4,
  continue.

If **no recipe or leaf wrote** in §3.3 (dry-run, or all divergences routed
to hand-off categories), skip §3.4 entirely — recomputing is a no-op.

#### 3.5 — Step 5: re-verify

Re-run the §3.1 SELECTs verbatim. Recompute divergences. Compare against the
pre-fix delta:

- `resolved` — divergence lines that dropped to zero delta.
- `residual` — still non-zero, with the new magnitude.
- `regressed` — was zero before, non-zero now (worst case; flag prominently).

Mark the account's `status`:

- `clean` — no divergences, no residual `PENDING`.
- `partially_resolved` — some divergences resolved, some residual.
- `unresolved` — no divergences resolved (leaf writes didn't move the needle).
- `regressed` — anything appeared in `regressed`.
- `failed` — a step raised.

### 4 — Aggregate

Roll the per-account results into `run_meta`:

- `accounts_total`, `accounts_clean`, `accounts_partially_resolved`,
  `accounts_unresolved`, `accounts_regressed`, `accounts_failed`.
- Global counts of leaf actions (promoted, inserted, deleted, flagged).
- List of accounts with **Human action required** (any account not `clean`).

### 5 — Write the report

Two files (one if `dry_run = true`):

- `reports/<date>_daily_btg_onshore.json` — the full structured report per
  the schemas in `references/step-schemas.md`.
- `reports/<date>_daily_btg_onshore.md` — human-readable summary. **Lead with
  the Human action required section**, then per-account tables.

Use `Write`. If a filesystem write tool is unavailable in the current session,
print the full report to chat and tell the user *"the orchestrator could not
write the report to disk — copy the content above into `<path>`"*. Never
silently drop the report.

### 6 — Escalate (file-only for now)

The report's **Human action required** section is non-empty for each account
whose `status ∈ {partially_resolved, unresolved, regressed, failed}`.

For each such account, include:

- one-sentence summary of what's still wrong (residual delta, failed leaf,
  regression)
- the SQL the analyst should re-run to inspect (paste the §3.1 SELECTs)
- the next leaf skill / external skill (e.g. `asset-register` for unmapped
  identifiers; `checkeddate-update` when the analyst is ready to advance the
  lock).

If the section is empty, it reads exactly: *"None — all clean. ✅"*

### 7 — Finalise

- If `dry_run = false` **and** at least one account completed §Step 5 without
  raising, write the state lock at
  `state/<date>_daily_btg_onshore.lock` (content: absolute path of the
  markdown report).
- If `dry_run = true`, do not write the lock.
- Reply in chat with:
  1. one-line status summary (`OK ✅ · N clean` / `⚠️ N residual` /
     `❌ N failed`)
  2. the report path
  3. the count of items in **Human action required**
  4. the resolved scope echo

## Critical rules

- **Writes to `Portfolio.AccountTransaction` are allowed** through two paths:
  (1) leaf-skill invocations (the standard route — each leaf carries its own
  guardrails); and (2) the orchestrator's own pre-classifier recipes documented
  under `references/` (currently only `deactivated-fund-residue.md`). Any
  direct-from-orchestrator write must be a documented recipe with a detection
  query and a distinct `[…]` `AgentCheck` tag. Ad-hoc writes are not allowed.
- **Act, don't ask.** When a recipe or leaf gives a high-confidence answer,
  apply it. Only pause for user input when the classifier is ambiguous or the
  leaves refuse. Report human-action items in the final report — do not
  interrupt the run to request approval mid-flight.
- **Never advance `CheckedDate`.** The lock is the safety net; advancing it is
  the analyst's approval step and is owned by the `checkeddate-update` /
  `execute_checked_date` specialist path. If a leaf reports it needed a lock
  move to complete its fix, that account is reported as `unresolved` — the
  analyst decides.
- **Dry-run is contagious.** If the orchestrator runs in `dry_run = true`,
  every leaf-skill invocation and pre-classifier recipe must also be in
  dry-run, and §3.4 (recompute) is skipped.
- **Continue-on-failure per account.** One account's failure never aborts the
  run. Failure is captured on the `AccountRun`, the loop continues.
- **Echo scope on every reply.** `(accounts, dry_run, force, max_accounts)`
  plus the count of late accounts resolved from Step 1.
- **Reply language matches the analyst.** PT or EN. JSON field names always
  English; markdown report headings can be PT for Sten readers.
- **PortfolioCreator is per-account, per-fix.** The user's routine is: fix →
  recompute → verify → next. Do not batch fixes across accounts and recompute
  once — the per-account feedback loop is what lets the loop continue with
  correct state.
- **Never reach back into a leaf skill's internals.** If a leaf's output shape
  doesn't match the schema, report *"unexpected shape from leaf X"* and ask
  the user to re-align `references/step-schemas.md`.

## When unsure

- **Audit query returns 0 late accounts.** Say so explicitly. Write a
  "nothing to do" report and finalise. This is a valid outcome.
- **A late account has `LastCustodyPositionDate = LastCheckedDate`.** Not
  actually late — filter it out at Step 1. This can happen at the exact
  moment the analyst just advanced the lock; benign.
- **Multiple `Activated=1` rows in `Portfolio.v_CheckedDate` for the same
  `(Account, Custody)`.** The proc's scalar subquery raises (error 512). Do
  not fix from here. Mark the account `status = "failed"` with reason
  `broken_lock` and include the duplicate-lock rows in the report.
- **A leaf reports `residual` after (A)–(E) but §3.5 verify shows the
  divergence is resolved.** Leaf-report bug or the divergence resolved via
  chain effects (PENDING promoted → downstream trade re-linked). Trust the
  verify — the divergence table is ground truth.
- **`pending-position-repair` returns `confidence = LOW`.** Do **not** write.
  Escalate as human-action with the leaf's ranked candidate list.
- **PortfolioCreator job returns `TIMEOUT`.** Do not retry from this skill.
  Mark the account failed and continue. Retry is a next-run decision.
- **The user asks to also advance CheckedDate as part of the routine.** Refuse
  politely and point at `checkeddate-update` / `execute_checked_date`. Doing
  it from here would collapse the safety net.
