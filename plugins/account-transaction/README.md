# account-transaction

Custody activity loaders for `Portfolio.AccountTransaction`. The plugin is a **container**: one
skill per custodian. Each skill knows how to read that custodian's activity (an export file **or an
API feed**), apply its field treatment, resolve assets / accounts / locks **live** through the
ayunit MCP, and insert the validated trades and cash/GL movements via
`Portfolio.AccountTransaction_Update @CMD='I'`.

Add a new custody later by dropping a new skill folder into `skills/` — you don't create another
plugin.

## Skills

| Skill | Custody | What it loads / fixes |
|---|---|---|
| `ubs-miami` | `UBS Miami` | UBS Online Services *Investment Activity* export (`.xls` all-accounts or per-account `.xlsx`) → trades (BUY/SELL/redemptions) + cash flows + GL/interest. Local parser → MCP `execute_procedure` (canary → batch). |
| `morgan-stanley` | `MS` | Morgan Stanley Online *All Activity* export (`.xlsx`, sheet `AllActivity`) → trades (Bought/Sold/Redemption) + in-kind transfers (ASSET RECEIPT/DELIVERY) + cash flows + GL (interest/dividend, fees, taxes, OVERNIGHT sweep). Internal bank-sweeps and personal banking are routed to `review`, never auto-booked. Local parser → MCP `execute_procedure` (canary → batch). |
| `btg-offshore` | `BTG Cayman` | BTG Offshore (Cayman) transactions pulled **live** from the ayunit MCP tool `get_btg_offshore_trades`, mapped by the local `scripts/parse_btg.py`, and committed **one account at a time through the ayunit `execute_batch`** (atomic). Security trades become BUY/SELL; cash/interest/fees become DEPOSIT/WITHDRAW/GENERAL LEDGER. Income (coupons, dividends, fund distributions) is booked as GL `INTEREST/DIVIDEND`, never a trade; NDF/FX-forward legs are Qty 1 @ 0 with the net cash from the FXNDF cash leg (`Forward Maturity` GL); futures move margin only (commission is the only cash); option-expiry side is taken from the held position. Auto-registers new TDs/NDFs (`Global.Asset_Update` + `Portfolio.AssetCustody_Update`). Dedups per account by `(tradeId, |value|)`, gates on `CheckedDate`, and lands unresolved rows as PENDING. Runs autonomously (idempotent, lock-safe) — default scope is all accounts for the latest 5 days. |
| `compromissada-fix` | `XP` / `BTG` (audit) | Batch fixer (not a loader). Normalises **COMPROMISSADA** (repo) trades onto Sten's cash-like convention: `Quantity` = financial value, `Price` = 1 on the BUY (and the tiny yield on the SELL). XP loads repos priced in the underlying debenture's units (`Quantity` = unit count, `Price` = unitPrice) — this skill audits, pairs BUY↔SELL, applies the **R6** fix lock-aware and pair-consistent, and reports rows it couldn't touch. BTG is verified but typically has nothing to fix. |

## Requirements

- **The ayunit MCP must be connected** in the session. Every DB read and write goes through it
  (`execute_select_query` for reads; `execute_procedure` for the UBS/MS writes; `execute_batch` for
  the BTG atomic per-account commit). The skills never hold credentials and never hit the DB any
  other way. `btg-offshore` also calls `get_btg_offshore_trades` on the same MCP (credential profile
  `BTG Sten Offshore`).
- **Python** for the local parsers. The Excel-based loaders (`ubs-miami`, `morgan-stanley`) need an
  Excel reader (`python -m pip install xlrd openpyxl`); `btg-offshore`'s `parse_btg.py` is pure
  stdlib (no external deps).

## How a load works

**UBS Miami / Morgan Stanley** (file → `execute_procedure`, canary → confirm → batch):
1. The parser reads the activity file and prints the lookup SELECTs it needs.
2. Claude runs those via the ayunit MCP and feeds the rows back to the parser.
3. The parser builds a reviewable plan (per-bucket counts, VALIDATED vs PENDING, cash recon).
4. Claude canaries one insert, you approve, then it batches the rest — duplicate-checked and
   lock-gated against `CheckedDate`. Unresolved assets and review items are booked PENDING (tracked,
   not dropped); only lock-blocked / unknown-account / zero-amount rows are reported, not written.

**BTG Offshore** (API feed → `execute_batch`, autonomous per-account commit):
1. Claude pulls the feed with `get_btg_offshore_trades` and saves the raw payload(s).
2. `parse_btg.py` prints the lookup SELECTs (assets, valid types, locks, held assets, existing
   transactions, inception seed); Claude runs them via the MCP and the parser assembles the lookups.
3. The parser maps every event, resolves option-expiry sides, applies coherence + the `CheckedDate`
   gate + `(tradeId,|value|)` dedup, and emits one `execute_batch` items file per account
   (registrations → AssetCustody maps → AccountTransaction inserts).
4. Claude commits each account atomically via `execute_batch` (dry-run validate → commit, assert
   `statements_run == len(items)`). Unresolved rows land PENDING; lock-blocked rows are excluded.

Just say something like *"load this UBS Miami export into AccountTransaction"*, *"load this
Morgan Stanley activity"*, or *"book the BTG Cayman trades for account 94137 this month"* — the
right loader triggers from how you phrase it.

## Adding a new custody loader

Each custodian has its own format and field rules, so each gets its own skill. Use any existing
skill as the template:

1. Create `skills/<custody-name>/` (kebab-case).
2. Copy a `parse_*.py` and adjust the custody-specific bits: the source (file columns or API
   sections), the activity/section → TransactionType map, the `CUSTODY` / `CURRENCY` constants,
   and any quirks (sweep identifiers, price scaling, settlement offset, dedup keys). The
   asset/account/lock **resolution logic and the MCP lookup pattern stay the same** — they're
   custody-parameterised via the `Custody = '<…>'` filters.
3. Write `SKILL.md` (copy a sibling; change the `name`, `description` triggers, custody label) and
   a `mapping.md` documenting that custodian's field treatment.
4. Keep the contract identical: parser does local-only work, all DB access via the ayunit MCP, only
   `VALIDATED`/`PENDING` rows written, never on/before a `CheckedDate`.
5. Re-package the plugin and reinstall.

## Design contract (shared by every skill here)

- **Single source of truth = the live DB via the ayunit MCP.** No hard-coded schema, account maps,
  or asset identifiers — all read live.
- **The parser never writes and never touches the DB.** It only reads the activity and applies the
  offline field treatment against MCP-gathered lookups.
- **Production safety:** per-row duplicate pre-check / per-account dedup (re-runs are idempotent),
  never write on/before an active `CheckedDate`, always pass absolute `Quantity`/`Price`/`Value`
  (the proc signs them), never pass `AccountCurrency`/`AccountFx`.

---
_Sten Capital · v0.8.1_
