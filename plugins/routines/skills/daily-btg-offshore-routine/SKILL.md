---
name: daily-btg-offshore-routine
description: "Use when the user wants to run the **daily BTG Offshore routine** end-to-end — load yesterday's BTG (Cayman) trades into Portfolio.AccountTransaction, triage stuck PENDING income receipts whose AssetRelated couldn't be inferred, reconcile any duplicate/restated trades, and produce a structured report of what landed, what was fixed, and what residual work still needs a human. This is an **orchestrator skill** (meta-skill): it does NOT touch the DB directly; it invokes the existing leaf skills account-transaction:btg-offshore, account-transaction:assetrelated-fix, and account-transaction:duplicate-trade-reconcile in sequence, captures structured JSON output between each step (schemas in references/step-schemas.md), branches on the results, writes a per-run JSON + markdown report under ~/Documents/sten-routines/reports/, and short-circuits idempotently on re-runs via a state lock under ~/Documents/sten-routines/state/. Designed for daily scheduled execution (Claude Cowork Desktop → Routines → Local, 08:00 BRT Mon–Fri) but equally runnable manually — fires on prompts like 'run the daily BTG routine for <date>', 'rode a rotina diária BTG de ontem', 'execute the daily-btg-offshore-routine in dry-run mode for 2026-06-25', 'roda a daily-btg-offshore-routine forçado para 2026-06-24'."
---

# Daily BTG Offshore routine — load, triage, reconcile, report

You are the orchestrator for Sten's daily BTG Offshore backoffice run. Each
business morning an analyst would manually pull yesterday's BTG (Cayman)
trades into `Portfolio.AccountTransaction`, look at what landed as `PENDING`,
chase any income receipts whose `AssetRelated` the loader couldn't infer
from the custody description, and check for restated / double-loaded trades.
This skill runs that whole flow autonomously and writes a report the analyst
reads instead of doing the work.

This is a **meta-skill / orchestrator**: it never reads or writes
`Portfolio.AccountTransaction` directly. It invokes the leaf skills below in
sequence, asks Claude to capture each leaf skill's result as structured
JSON, branches on the JSON, and writes a final report. Every guardrail
(lock awareness, dedup, sign conventions, `AgentCheck` audit trail) is the
job of the leaf skill that does the write — the orchestrator's only job is
to chain them sanely and explain the outcome.

## Leaf skills it invokes (in order)

| # | Skill | Purpose |
|---|---|---|
| 1 | `account-transaction:btg-offshore` | Pull yesterday's BTG Offshore trades from the ayunit MCP via `get_btg_offshore_trades`, map them with `parse_btg.py`, and commit per-account via `execute_batch`. |
| 2 | `account-transaction:assetrelated-fix` | Fill `AssetRelated` on `GENERAL LEDGER RECEIPT` rows whose `GeneralLedgerType = 'INTEREST/DIVIDEND'` and promote stuck PENDING income to UPDATED. |
| 3 | `account-transaction:duplicate-trade-reconcile` | Detect and (lock-aware) remove restated / double-loaded trades against `Portfolio.v_CustodyPosition`. |

Each leaf skill's own `SKILL.md` is **the** authority for its inputs,
outputs, and guardrails. This orchestrator must not duplicate that logic —
it only decides scope and reads the leaf skill's structured result.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `date` | yesterday (BRT business day — Mon defaults to Fri) | One date per run; routine is per-day. |
| `accounts` | all BTG Cayman accounts | Narrow to one account or a list for testing. |
| `dry_run` | `false` | `true` → every leaf skill is also dry-run / read-only; nothing is written; no state lock. |
| `force` | `false` | `true` → ignore the day's state lock and run anyway. Use only on a confirmed crash mid-way. |

**Echo the resolved `(date, accounts, dry_run, force)` at the start of every
reply.** If `dry_run = true`, prefix every status line with `[DRY-RUN]`.
Reply in the analyst's language (PT or EN); keep JSON field names in English.

## State / idempotency

State and report locations (try in order; the first that exists wins,
otherwise create the OneDrive-synced one for a Sten analyst):

1. `%USERPROFILE%/OneDrive - STEN/Documents/sten-routines/` (the synced home — preferred)
2. `%USERPROFILE%/Documents/sten-routines/` (local fallback)

Subfolders: `state/`, `reports/`.

Before doing anything:

- If `~/.../state/<date>_daily_btg.lock` exists **and** `force = false`:
  short-circuit. Read the previous report at
  `~/.../reports/<date>_daily_btg.md` and reply with *"Already ran for
  `<date>` — here's the report"* followed by the **Human action required**
  section from that report. **Do not** invoke any leaf skill.
- If `dry_run = true`, never read or write the state lock; you may overwrite
  an existing dry-run report file in place.
- The lock is written only at the end of a successful real run (§8). Its
  content is just the absolute path of the report file.

## The routine

### 1 — Pre-flight (always; refuse to proceed on any failure)

1. **MCP reachable.** Run a trivial `execute_select_query` (e.g. `SELECT 1`).
   If it fails, **abort with a clear message** — do not call any leaf
   skill without the MCP connected.
2. **Resolve scope.** Echo `(date, accounts, dry_run, force)` back. If
   `date` is a weekend or known holiday, walk back to the previous business
   day and tell the user *"resolved to Fri 2026-06-26"*.
3. **State lock.** Apply the §state rules above.
4. **Skeleton report** — initialise the in-memory report shape from
   `references/step-schemas.md` (`run_meta.started_at = <now>`, every step
   `status = "not_run"`).

### 2 — Step 1: BTG load

Invoke `account-transaction:btg-offshore` with the resolved scope. Make
`dry_run` explicit in the prompt to that skill — if dry-run, ask it to
**stop after the parser's plan** (no `execute_batch`).

Capture the result as JSON matching the `step1_btg_load` schema in
`references/step-schemas.md`:

- `accounts_attempted` (count + list)
- `rows_inserted_validated` (count)
- `rows_inserted_pending` (count)
- `rows_lock_blocked` (object: `{count, by_account: {acc: n, …}}`)
- `unresolved_assets` (list of objects: `{tradeId, description, identifier}`)
- `accounts_with_zero_activity` (list)
- `auto_registered_assets` (list — when btg-offshore auto-creates TDs/NDFs)
- `errors` (list of strings; empty on success)

**Hard-error stop:** if step 1 returns one or more errors, **skip steps 2
and 3** and jump to §6 (write the report). The downstream fixers operate
on stale state and could make the situation worse; the analyst must see
the load failure first.

### 3 — Step 2: triage PENDING income

First, decide whether the step has anything to do. Read-only check
(orchestrator-side; doesn't count as a leaf-skill invocation):

```sql
SELECT COUNT(*) AS n
FROM Portfolio.v_AccountTransaction
WHERE Custody = 'BTG' AND Date = '<date>' AND Status = 'PENDING'
  AND GeneralLedgerDescription LIKE '%INTEREST%' OR GeneralLedgerDescription LIKE '%DIVIDEND%'
  AND AssetRelated IS NULL;
```

(The exact column predicate depends on how BTG offshore stores the type —
consult the `assetrelated-fix` `SKILL.md` for the authoritative scope query
and use that one verbatim.)

- `n = 0` → record `step2_assetrelated = {status: "skipped", reason: "no candidates"}` and move on.
- `n > 0` → invoke `account-transaction:assetrelated-fix` scoped to
  `Custody = 'BTG', Date = <date>`, with the same `dry_run` flag.

Capture as JSON matching `step2_assetrelated`:

- `candidates` (count)
- `promoted_to_updated` (count)
- `residual_pending` (count + list of `{pk, asset_guess, confidence}`)
- `low_confidence_skipped` (count + list)
- `errors` (list)

### 4 — Step 3: duplicate reconcile

Invoke `account-transaction:duplicate-trade-reconcile` scoped to
`Custody = 'BTG', Date = <date>`, with the same `dry_run` flag.

Capture as JSON matching `step3_duplicates`:

- `pairs_evaluated` (count)
- `duplicates_removed` (count + list of removed `pk`s)
- `suspicious_pairs_flagged` (count + list of `(pk_a, pk_b, reason)`)
- `errors` (list)

### 5 — Verify (read-only; orchestrator-side)

Post-run sanity SELECTs on the same scope. The orchestrator runs these
itself via `execute_select_query` — they do not invoke any leaf skill.

```sql
-- Final per-status count for the BTG date
SELECT Status, COUNT(*) AS n
FROM Portfolio.v_AccountTransaction
WHERE Custody = 'BTG' AND Date = '<date>'
GROUP BY Status;

-- Residual PENDING after triage (worst first)
SELECT pk_AccountTransactionID, ClientAccount, Asset, GeneralLedgerDescription, Value
FROM Portfolio.v_AccountTransaction
WHERE Custody = 'BTG' AND Date = '<date>' AND Status = 'PENDING'
ORDER BY ABS(Value) DESC;
```

Capture as JSON matching `verify`:

- `status_counts` (object: `{VALIDATED, UPDATED, PENDING, IGNORED}`)
- `residual_pending_count`
- `residual_pending_rows` (list of `{pk, account, asset, description, value}`)
- `residual_pending_lock_blocked_count` (subset of the above — copy from
  `step1.rows_lock_blocked`, these are expected and not a human-action item)

### 6 — Write the report

Two files (one if `dry_run = true` — only the markdown):

- `~/.../reports/<date>_daily_btg.json` — the full structured report
  (`run_meta`, `step1_btg_load`, `step2_assetrelated`, `step3_duplicates`,
  `verify`). Field shapes from `references/step-schemas.md`.
- `~/.../reports/<date>_daily_btg.md` — the human-readable summary,
  templated in `references/step-schemas.md`. **Lead with the Human action
  required section** (empty if clean), then per-step tables.

Use the tools available in the current session to write files
(`Write` / `execute_select_query` etc.). If you don't have a filesystem
write tool available, print the full report content to chat **and** tell
the user *"the orchestrator could not write the report to disk in this
session — copy the content above into <path>"*. Do not silently drop the
report.

### 7 — Escalate (file-only for now)

The report's **Human action required** section is non-empty if any of:

- `step1 / step2 / step3.errors` is non-empty
- `verify.residual_pending_count - verify.residual_pending_lock_blocked_count > 0`
- `step3.suspicious_pairs_flagged > 0`
- `step1.unresolved_assets` is non-empty

For each item, include:

- one-sentence summary of what's wrong
- the SQL the analyst should run to investigate (orchestrator helps draft it)
- the next leaf skill / external skill they'd likely use (e.g.
  `asset-register` for `unresolved_assets`)

If none of the above triggered, the section reads exactly:
*"None — all clean. ✅"*

### 8 — Finalise

- If `dry_run = false` **and** no fatal error from §5 verify, write the
  state lock at `~/.../state/<date>_daily_btg.lock` (content: the absolute
  path of the markdown report).
- If `dry_run = true`, do not write the lock.
- Reply in chat with:
  1. one-line status summary (`OK ✅` / `OK with residuals ⚠️` / `FAIL ❌`)
  2. the report path
  3. the count of items in **Human action required**
  4. the resolved scope echo

## Critical rules

- **Never read or write `Portfolio.AccountTransaction` directly.** Every
  mutation goes through a leaf skill. Only the read-only `execute_select_query`
  is allowed from the orchestrator, and only for the §3 candidate check and
  the §5 verify queries.
- **Dry-run is contagious.** If the orchestrator runs in `dry_run = true`,
  every leaf-skill invocation must also be in dry-run. Never pass
  `dry_run = false` to a leaf skill while the orchestrator is in dry-run.
- **Stop on step-1 hard error.** If `btg-offshore` errors out, the
  downstream fixers operate on stale state — skip them entirely and write
  the report flagging the load failure as the top human-action item.
- **Echo scope on every reply.** `(date, accounts, dry_run, force)` plus the
  resolved business day. Both the chat reader and the report header need it.
- **Reply language matches the analyst.** PT or EN. JSON field names always
  English; markdown report headings can be PT for Sten readers.
- **Lock-blocked PENDING is not a human-action item.** It's the expected
  outcome whenever a trade comes in dated on/before an active CheckedDate.
  Separate it in the residual section.
- **Never reach back into a leaf skill's internals.** If a leaf skill's
  output shape changes, the orchestrator reports *"got an unexpected shape
  from step X"* and asks the user to re-align the schema in
  `references/step-schemas.md`. The orchestrator never patches over the gap.

## When unsure

- **MCP not reachable** → abort §1. Do not retry blindly. Report
  *"ayunit MCP unreachable — analyst must verify the local MCP and re-run."*
- **State lock exists but the report file is missing** → previous run
  crashed mid-way. Do not auto-retry. Surface to the user: *"state lock
  present for `<date>` but the report is missing. Re-run with `force=true`
  to redo it, or remove the stale lock at `<path>` and decide manually."*
- **Step 1 reports `unresolved_assets > 0`** → don't try to register them
  from the orchestrator (the `asset-register` skill isn't in the routine's
  leaf set). List them in the human-action section.
- **Step 2 reports `low_confidence_skipped > 0`** → income receipts whose
  ticker the heuristic couldn't confirm. List with their `pk`s in the
  human-action section; the analyst either confirms manually or extends
  `resolve_assetrelated.py`.
- **Step 3 reports `suspicious_pairs_flagged > 0`** → the leaf skill refused
  to act on its own. List both `pk`s and the identifying fields; never
  attempt the delete from the orchestrator.
- **The `(date, accounts, dry_run, force)` the user typed disagrees with
  the schedule's default** → trust the user's explicit input; just echo
  the resolved scope back so they can spot a typo.
