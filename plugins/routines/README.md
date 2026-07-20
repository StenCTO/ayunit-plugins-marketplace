# routines

Orchestrator skills (meta-skills) for Sten's daily and weekly backoffice
routines. Each skill in here chains the **leaf skills** from `account-transaction`
and `position` into a defined sequence — load, audit, fix, reconcile, report —
and behaves like the analyst would: invokes the next step only when the
previous one looks right, branches on errors, escalates residuals.

These orchestrators are designed to fire from **Claude Cowork Desktop →
Routines → New routine → Local** (local scheduled tasks have full access to
the ayunit MCP and the analyst's installed plugins). They are equally usable
manually — just type the trigger phrase in chat.

## Skills

| Skill | Cadence | What it does |
|---|---|---|
| `new-asset-onboarding` | Daily, 07:30 BRT Mon–Fri (before the custody routines) | Sweeps a rolling window of recent `Portfolio.AccountTransaction` rows **across every custody** for assets not yet in `Global.Asset` or not yet mapped in `Portfolio.AssetCustody`. Chains `account-transaction:transaction-workday-audit` → `asset:asset-lookup` → `asset:register-br-funds` / `asset:asset-register` (peer-analogy) → `asset:assetcustody-fill` → `account-transaction:pending-revalidate` → `asset:asset-price-history`. Autonomous: BR-fund CNPJs go through the ANBIMA-enriched chain; other kinds go through peer-analogy classification; anything the leaf refuses is logged for the analyst. Reports written to `~/Documents/sten-routines/reports/` **and** uploaded to Azure Blob via `mcp__ayunit__upload_blob_file`. Idempotent (state lock per end-date). |
| `daily-btg-onshore-routine` | Daily, 08:00 BRT Mon–Fri | Enumerates BTG onshore accounts whose `CheckedDate` lags behind the latest `CustodyPosition` snapshot, walks each through an `AccountPosition ↔ CustodyPosition` reconciliation, routes each defect to the appropriate leaf skill (`asset-register`, `pending-revalidate`, `pending-position-repair`, `assetrelated-fix`, `duplicate-trade-reconcile`, `position-quantity-adjustment`), re-runs the PortfolioCreator between fixes, and emits a per-run JSON + markdown report to `~/Documents/sten-routines/reports/`. Idempotent (state lock per date). Never advances `CheckedDate` — that's the analyst's approval step. |
| `daily-btg-offshore-routine` | Daily, 08:15 BRT Mon–Fri | Loads yesterday's BTG (Cayman) trades via `account-transaction:btg-offshore`, triages stuck PENDING income receipts via `assetrelated-fix`, reconciles duplicates via `duplicate-trade-reconcile`, produces a per-run JSON + markdown report. Idempotent (state lock per date). |

More routines (Morgan Stanley daily, UBS Miami daily, weekly position
reconcile, monthly compromissada audit) will be added as the pattern
extends.

**Recommended chain order for a full daily run:** `new-asset-onboarding`
first (fixes master-data gaps across all custodies), then the custody-
specific routines (which now find zero unmapped assets and can focus on
position reconciliation).

## Design contract

- **Orchestrators never touch the DB directly.** Every read goes through
  `execute_select_query` on the ayunit MCP for read-only verify queries; every
  write is delegated to a leaf skill from `account-transaction` /
  `position`. The leaf skill is the source of truth for its own guardrails
  (lock-awareness, dedup, sign conventions, AgentCheck audit trail).
- **Sequence, capture, branch, report.** Each orchestrator's job is to:
  call leaf skill N, ask Claude to capture the result as structured JSON
  matching a schema in `references/step-schemas.md`, decide whether to run
  step N+1, and accumulate a per-run report.
- **Dry-run contagious.** If the orchestrator is invoked in `dry_run` mode,
  every leaf skill it calls must also be in dry-run / read-only mode. No
  half-runs.
- **Idempotent re-runs.** Each routine writes a state lock per business day
  under `~/Documents/sten-routines/state/`. A second trigger on the same date
  short-circuits to "already ran today" unless `force=true`.
- **Reports go to disk, escalations are file-only (for now).** Per-run JSON
  + markdown under `~/Documents/sten-routines/reports/`. Slack/email
  integration comes later — for now the analyst reads the report.

## Scheduling (Claude Cowork Desktop)

1. Install this plugin: `/plugin install routines@sten-ayunit`.
2. Open **Routines → New routine → Local**.
3. Cron: `0 8 * * 1-5` (08:00 weekdays).
4. Prompt: *"Run the routines:daily-btg-onshore-routine skill for yesterday's date."*

Caveat: Desktop scheduled tasks only fire **while Claude Cowork Desktop is
open and the computer is awake** — if the analyst's laptop is closed at
08:00, the task is skipped (no auto-retry). For 24/7 unattended runs, the
canonical workaround is to expose the ayunit MCP as a remote HTTPS server
and migrate to `/schedule` (Anthropic-hosted) — out of scope here.

## Adding a new routine

1. Create `skills/<routine-name>/` (kebab-case).
2. Write `SKILL.md`:
   - Frontmatter `name` + a thorough `description` (trigger phrases in PT
     and EN — the description is what fires the skill).
   - Define inputs (with defaults), state-lock path, the step-by-step
     sequence, the JSON capture between steps, the report layout, the
     escalation rules.
3. Drop `references/step-schemas.md` describing every JSON capture shape
   and the markdown report template.
4. Bump this plugin's `version` and push to both remotes (Azure + GitHub).
5. Add a row to the Skills table above.
6. Register a new Desktop scheduled task with the appropriate prompt.

---
_Sten Capital · v0.3.0_
