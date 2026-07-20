# account-transaction

Toolkit for **`Portfolio.AccountTransaction`** on the ayunit MCP — an activity loader for the
custodies still handled here plus a **custody-agnostic** audit + fix suite that keeps the tape
clean. Every DB read and write goes through the ayunit MCP: no bundled credentials, no hard-coded
schema, no direct connections.

Add a new custody loader later by dropping a new skill folder into `skills/` — you don't create
another plugin. The audit / fix skills are already custody-agnostic and pick up the new custody
automatically.

## Skills

| Skill | Kind | What it does |
|---|---|---|
| `ubs-miami` | Loader (`UBS Miami`) | UBS Online Services *Investment Activity* export (`.xls` all-accounts or per-account `.xlsx`) → trades (BUY/SELL/redemptions) + cash flows + GL/interest. Local parser → MCP `execute_procedure` (canary → confirm → batch). |
| `transaction-workday-audit` | Executing orchestrator (custody-agnostic) | Single entry point for the analyst's workday audit + fix cycle on the recent tape. **Check 1** — four-stage onboarding-pipeline detector (`Global.Asset` missing, `AssetCustody` mapping missing, `CustodyPosition` back-fill needed, `AssetData.Price` back-fill needed) that, if any sub-detector has findings, invokes `routines:new-asset-onboarding` **exactly once** and re-probes per-stage. **Check 2** — six-step sequential fix orchestrator that invokes `compromissada-fix` → `assetrelated-fix` → `pending-revalidate` → `pending-position-repair` → `duplicate-trade-reconcile` → `position-quantity-adjustment` in order, detect / skip-if-empty / invoke / re-probe / pause between each step. Writes only through invoked siblings (each with its own preview/confirm/execute + CheckedDate lock enforcement). The audit itself never calls `execute_procedure` and never moves a `CheckedDate`. |
| `pending-revalidate` | Fix (custody-agnostic) | Re-invokes `Portfolio.AccountTransaction_Update @CMD='U'` on `PENDING` rows whose prerequisites are now available, so the procedure's built-in auto-validators (auto-match `Asset`, auto-fill `Price`, auto-fill `Quantity`) re-fire and promote the row to `UPDATED`. Takes a pk list (audit hand-off shape) or a scoped filter. SELECT-first-merge, `AccountCurrency`/`AccountFx` dropped, absolute values, `RawTransaction` preserved, lock-gated against `CheckedDate`, atomic `execute_batch` dry-run → commit → verify. |
| `pending-position-repair` | Fix (custody-agnostic) | Repairs `PENDING` rows whose `RawTransaction` carries no valid asset identifier by inferring the intended `(Asset, direction)` from the `AccountPosition ↔ CustodyPosition` delta on the same account/date. Ranks candidate tuples with a confidence score; on HIGH confidence with a clean reconciliation, issues `@CMD='U'` (SELECT-first-merge, lock-gated, `AgentCheck [PR-POS]` tag). MED/LOW confidence → surfaces ranked candidates for analyst review, writes nothing. Sibling of `pending-revalidate`. |
| `duplicate-trade-reconcile` | Fix (custody-agnostic) | Reconciles candidate duplicate trades against the day-over-day quantity delta in `Portfolio.CustodyPosition` and deletes the duplicates it can prove with high confidence via `@CMD='D'` (dry-run first). Anything it can't prove is reported for human review. |
| `assetrelated-fix` | Fix (custody-agnostic) | Fixes `INTEREST/DIVIDEND` receipts with missing `AssetRelated` — coupons, dividends, *rendimentos*, fee rebates whose paying security the loader couldn't resolve, so the income never ties to an asset in return attribution. Parses the description (three income layouts), confirms against the account's holding universe (or against a global ISIN/CUSIP index for identifier-based custodies), auto-writes high-conviction matches, reports the rest. |
| `compromissada-fix` | Fix (`XP` / `BTG` audit) | Normalises **COMPROMISSADA** (repo) trades onto Sten's cash-like convention (`Quantity` = financial value, `Price` = 1 on the BUY, tiny yield on the SELL). XP loads them in the underlying debenture's units — this skill audits, pairs BUY↔SELL, applies the **R6** fix lock-aware and pair-consistent, and reports rows it couldn't touch. |
| `position-quantity-adjustment` | Fix (custody-agnostic) | Sweep absorber for the **fractional dust** that survives the daily `CustodyPosition` ↔ `AccountPosition` reconciliation — sub-1-unit fund quota drift and sub-currency-unit cash rounding gaps. Kind-aware tolerances, non-cash → `BUY`/`SELL` with no cash leg, cash → `GL RECEIPT`/`DELIVERY` for the full amount. Lock-aware; must run **before** the daily `CheckedDate` advances over the target date. |

## Requirements

- **The ayunit MCP must be connected** in the session. Every DB read and write goes through it —
  `execute_select_query` for reads, `execute_procedure` / `execute_batch` for writes. The skills
  never hold credentials and never hit the DB any other way.
- **Python** for the local parsers. The Excel-based loader (`ubs-miami`) needs an Excel reader
  (`python -m pip install xlrd openpyxl`).

## How the pieces fit together

**Loader path** (`ubs-miami`, file → `execute_procedure`, canary → confirm → batch):

1. The parser reads the activity file and prints the lookup SELECTs it needs.
2. Claude runs those via the ayunit MCP and feeds the rows back to the parser.
3. The parser builds a reviewable plan (per-bucket counts, VALIDATED vs PENDING, cash recon).
4. Claude canaries one insert, you approve, then it batches the rest — duplicate-checked and
   lock-gated against `CheckedDate`. Unresolved assets and review items land `PENDING` (tracked,
   not dropped); only lock-blocked / unknown-account / zero-amount rows are reported, not written.

**Workday hygiene path** (`transaction-workday-audit` = the executing orchestrator):

1. Analyst runs `transaction-workday-audit` (default: last 7 days on `Date`, book-wide).
2. **Check 1** detects the four onboarding-pipeline gaps (`Global.Asset` missing, `AssetCustody`
   mapping missing, `CustodyPosition` back-fill needed, `AssetData.Price` back-fill needed) and,
   if any gap has findings, invokes `routines:new-asset-onboarding` **exactly once** to execute
   the four-stage fix. Re-probes each sub-detector and reports the per-stage delta.
3. **Check 2** invokes six fix sibling skills in a fixed order —
   `compromissada-fix` → `assetrelated-fix` → `pending-revalidate` → `pending-position-repair` →
   `duplicate-trade-reconcile` → `position-quantity-adjustment` — with detect / skip-if-empty /
   invoke / re-probe / pause between each step.
4. Every write goes through the invoked sibling's own preview/confirm/execute cycle. The audit
   itself never writes directly and never moves a `CheckedDate` — each sibling enforces the lock
   on its own writes; `lock-blocked` rows are surfaced for the analyst's decision.

Just say something like *"load this UBS Miami export into AccountTransaction"*, *"audit the tape
for last week"*, or *"re-validate PENDING pks 91181, 91182, 91183, 91184"* — the right skill
triggers from how you phrase it.

## Adding a new custody loader

Each custodian has its own format and field rules, so each loader gets its own skill.

1. Create `skills/<custody-name>/` (kebab-case).
2. Write a `parse_<custody>.py`: the source (file columns or API sections), the activity → `TransactionType` map, the `CUSTODY` / `CURRENCY` constants, and any quirks (sweep identifiers, price scaling, settlement offset, dedup keys). The asset / account / lock **resolution logic and the MCP lookup pattern stay the same** — they're custody-parameterised via the `Custody = '<…>'` filters.
3. Write `SKILL.md` (copy `ubs-miami/SKILL.md`; change the `name`, `description` triggers, custody label) and a `mapping.md` documenting the field treatment.
4. Keep the contract identical: parser does local-only work, all DB access via the ayunit MCP, only `VALIDATED` / `PENDING` rows written, never on/before an active `CheckedDate`.
5. Re-package the plugin and reinstall.

The audit / fix skills are custody-agnostic and pick up the new custody automatically — you do not
need to change them when adding a loader.

## Design contract (shared by every skill here)

- **Single source of truth = the live DB via the ayunit MCP.** No hard-coded schema, account maps,
  or asset identifiers — all read live.
- **Local parsers never write and never touch the DB.** They only read the activity and apply the
  offline field treatment against MCP-gathered lookups.
- **Production safety:** per-row duplicate pre-check / per-account dedup (re-runs are idempotent),
  never write on/before an active `CheckedDate`, always pass absolute `Quantity`/`Price`/`Value`
  (the proc signs them), never pass `AccountCurrency`/`AccountFx`, always preserve `RawTransaction`
  on updates, always set `AgentCheck` on non-loader writes.
- **Audit orchestrates, siblings write.** `transaction-workday-audit` is the workday driver:
  Check 1 delegates to `routines:new-asset-onboarding`, Check 2 chains the six fix siblings in
  order. The audit itself never calls `execute_procedure` / `execute_batch(dry_run=false)` and
  never moves a `CheckedDate` — every write and every lock check lives inside the invoked sibling.

---
_Sten Capital_
