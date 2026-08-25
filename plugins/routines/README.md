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
| `new-asset-onboarding` | Daily, 07:30 BRT Mon–Fri (before the custody routines) | Sweeps a rolling window of recent `Portfolio.AccountTransaction` rows **across every custody** for the four onboarding-pipeline gaps. **Detects** by re-executing the SQL of `account-transaction:transaction-workday-audit` Check 1's four sub-detectors directly (1a needs-registration / 1b needs-mapping-only / 1c needs-position-backfill / 1d needs-price-backfill) — does NOT invoke the audit via the `Skill` tool, per the no-loop reciprocity convention (the audit's Check 1 invokes THIS skill). **Fixes** by chaining `asset:asset-lookup` (1a safety pre-check) → `asset:register-br-funds` / `asset:asset-register` (peer-analogy) → `asset:assetcustody-fill` (mapping + Update_Missing_Asset back-fill) → `account-transaction:pending-revalidate` → `asset:asset-price-history`. Autonomous: BR-fund CNPJs go through the ANBIMA-enriched chain; other kinds go through peer-analogy classification; anything the leaf refuses is logged for the analyst. Also supports **ad-hoc per-account mode** via `account_filter`: applies to every detector query, skips the daily state lock, writes under `reports/ad-hoc/` — used by the audit's Check 1 executing form when it needs to fix a single account's gaps without a fleet-wide sweep. Reports written to `~/Documents/sten-routines/reports/` **and** uploaded to Azure Blob via `mcp__ayunit__upload_blob_file`. Idempotent (state lock per end-date). |
| `daily-btg-onshore-routine` | Daily, 08:00 BRT Mon–Fri | Enumerates BTG onshore accounts whose `CheckedDate` lags behind the latest `CustodyPosition` snapshot, walks each through an `AccountPosition ↔ CustodyPosition` reconciliation, routes each defect to the appropriate leaf skill (`asset-register`, `pending-revalidate`, `pending-position-repair`, `assetrelated-fix`, `duplicate-trade-reconcile`, `position-quantity-adjustment`), re-runs the PortfolioCreator between fixes, and emits a per-run JSON + markdown report to `~/Documents/sten-routines/reports/`. **v0.8.0**: autonomous **price-backfill** pass (§3.3.5) that batch-INSERTs missing `AssetData.Price` rows from `v_CustodyPosition` for held assets (closes BTG-issued instrument pricing gaps — COEs, CDBs, CRIs, CRAs); autonomous **residual auto-plug** pass (§3.6) that writes synthetic BUY/SELL rows within tight tolerance (`|dQty|/cust_qty < 0.1%` OR dust on no-longer-held, `|dQty|×Price < R$500`) with `[POS-PLUG]` / `[POS-PLUG-DUST]` tags — cash-side residuals always escalate. **v0.7.0**: default `end_date = D-1`; 9-digit account normalization; opt-in `refresh_from_btg` Step 0; access_name cache; progressive `[STEP N]` chat output; `fund-incorporation-duplicate` pre-classifier recipe. Idempotent (state lock per date). Never advances `CheckedDate`. |
| `daily-btg-offshore-routine` | Daily, 08:15 BRT Mon–Fri | Loads yesterday's BTG (Cayman) trades via `account-transaction:btg-offshore`, triages stuck PENDING income receipts via `assetrelated-fix`, reconciles duplicates via `duplicate-trade-reconcile`, produces a per-run JSON + markdown report. Idempotent (state lock per date). |
| `btg-onshore-onboarding` | One-shot per custody × cutoff (runs BEFORE `daily-btg-onshore-routine` takes over) | Enumerates every BTG onshore ClientAccount with non-zero `v_CustodyPosition` rows on the cutoff date but no `CheckedDate` yet, and walks each through the full inception cycle: pre-flight → BTG feed pull → asset triage (`asset-lookup` → `assetcustody-fill` → `asset-register` → `asset-price-history`) → seed build per the codified XP-tested recipe → canary + batched `AccountPosition_Update I` inserts → custody reconcile → autonomous `execute_checked_date` advance on clean reconcile → post-cutoff PENDING sweep via `pending-revalidate`, `pending-position-repair`, `duplicate-trade-reconcile`. Tranche-disciplined (pilot 3 first, then ~20 per tranche with per-tranche checkpoint report + XLSX tracker). Ledger-based resume story. **Diverges from `daily-btg-onshore-routine` in one place**: it advances `CheckedDate` autonomously (inception IS by definition the creation of the first lock, and the pre-flight + post-seed reconcile gates replace the analyst's manual lock-move review). |

More routines (Morgan Stanley daily, UBS Miami daily, weekly position
reconcile, monthly compromissada audit) will be added as the pattern
extends.

**Recommended chain order for a full daily run:** `new-asset-onboarding`
first (fixes master-data gaps across all custodies), then the custody-
specific routines (which now find zero unmapped assets and can focus on
position reconciliation).

**Onboarding a new BTG onshore account (or a batch of them)**:
`btg-onshore-onboarding` is the one-shot pipeline that runs BEFORE any
of the above take effect for the new accounts. Once every account in
the backlog reaches `clean_locked`, the daily routines pick them up on
their next run.

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
_Sten Capital · v0.8.0_
