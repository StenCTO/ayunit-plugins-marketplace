# position

Position monitoring & reconciliation tools for the Sten book of record. Centred
on two Portfolio views accessed live through the ayunit MCP:

| View | Role |
|---|---|
| `Portfolio.v_AccountPosition` | **Book of record.** Internal end-of-day position derived from `AccountTransaction` + corporate-action processing. The truth Sten reports on. |
| `Portfolio.v_CustodyPosition` | **Broker snapshot.** What the custodian says we hold (loaded per custody — UBS, MS, BTG, XP …). Reconciliation target. |

> The custody ↔ asset mapping (`Portfolio.v_AssetCustody`) — required for any
> custody-feed position to resolve to an `Asset` — lives in the **`asset`**
> plugin, alongside `Global.Asset`.

The plugin is a **container**: one skill per workflow (reconcile a custody,
audit a position drift, register a missing custody mapping, …). Each skill
keeps the same contract as `account-transaction`: parser/orchestrator runs
locally, **all DB access goes through the ayunit MCP**, writes are
lock-aware and idempotent.

## Skills

| Skill | Scope | What it does |
|---|---|---|
| `inception-position` | Account onboarding | Seeds the **Inception Position** — the first `Portfolio.AccountPosition` rows for a newly-onboarded `(Account, Custody)` pair on its cutoff date (Open=Close, zero flows). Runs an exhaustive pre-flight (refuses if any position or CheckedDate already exists), canaries one row, then batches the rest via `Portfolio.AccountPosition_Update @CMD='I'`, verifies, and reconciles against custody. Does **not** create the CheckedDate lock — after positions are in and validated, the user adds it manually to freeze the seed. |

## Requirements

- **The ayunit MCP must be connected** in the session. Every read goes through
  `execute_select_query`; every write through the appropriate
  `*_Update` procedure (`Portfolio.AssetCustody_Update`, etc.) — never direct
  DML. The skills never hold credentials and never hit the DB any other way.
- Reference docs live on the same MCP — `search_docs` / `read_doc` for
  `portfolio-creator/pipeline`, `checkeddate/usage`, and any position-specific
  pages.

## Design contract (shared by every skill here)

- **Single source of truth = the live DB via the ayunit MCP.** No hard-coded
  schema, account maps, or asset identifiers — all read live.
- **Reads are the default; writes are explicit.** Reconciliation skills produce
  a reviewable report first; any corrective write (e.g. registering an
  `AssetCustody` mapping) goes through the procedure with the same guardrails
  as `account-transaction` (SELECT-first-merge, absolute values, AgentCheck).
- **Lock-aware:** writes that touch `Portfolio.AccountTransaction` (e.g. fixing
  a position by inserting/adjusting a trade) must respect the per-(Account,
  Custody) `CheckedDate` lock — same contract as `account-transaction`.
- **Idempotent re-runs.** Reports and corrective batches can be re-run without
  duplicating writes.

## Adding a new skill

1. Create `skills/<skill-name>/` (kebab-case).
2. Write `SKILL.md` with frontmatter (`name`, `description` — the
   description's `Use when …` triggers are what fire the skill in chat).
3. If the skill scripts anything (parser, mapper), drop the script alongside
   `SKILL.md` and keep DB access strictly through the MCP.
4. Bump this plugin's `version` in `.claude-plugin/plugin.json` and push to
   both remotes (Azure + GitHub) so teammates pick it up via
   `/plugin marketplace update sten-ayunit`.

---
_Sten Capital · v0.1.0 (draft)_
