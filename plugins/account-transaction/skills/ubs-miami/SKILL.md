---
name: ubs-miami
description: Use when the user wants to load a UBS Miami custody activity export (the "Investment Activity" .xls or per-account .xlsx) into Portfolio.AccountTransaction — parse the file, apply every field treatment (account map, TransactionType, CUSIP→Asset resolution, bond per-100 vs ETF per-share price, sign handling, OVERNIGHT sweep interest), lock-gate against CheckedDate, then insert the trades and cash/GL movements via Portfolio.AccountTransaction_Update @CMD='I'. A repeatable custody loader, run per export file.
---

# Load UBS Miami activity into `Portfolio.AccountTransaction`

You are the orchestrator that turns a **UBS Miami *Investment Activity* export** (the legacy
all-accounts `.xls`, or a per-account `.xlsx` such as `AE23928.xlsx` — both share the same
columns) into validated `Portfolio.AccountTransaction` rows. The custody is `UBS Miami`; the
accounts are USD / offshore. A full load covers the **trades** (BUY / SELL / redemptions) **and**
the cash/GL movements in the file (deposits, withdrawals, interest, fees) — the GL/interest rows
always load, they are not optional. **No movement is silently dropped:** a row that can't be fully
mapped/validated is still booked `PENDING` (so it stays tracked in `AccountTransaction`); only
genuinely un-writable rows (lock-blocked, unknown account, zero amount) are left out.

## Single source of truth — read this first

- **The ayunit backend / live DB is the one source of truth.** This skill never hard-codes schema,
  account maps, or asset identifiers — it reads them live through the **ayunit MCP**.
- **All DB access goes through the ayunit MCP tools** — `execute_select_query` for every read
  (asset/account/lock lookups, dedup, verify) and `execute_procedure`
  (`Portfolio.AccountTransaction_Update`, `cmd='I'`) for every write. That is the only path.
- **`parse_ubs.py` does only LOCAL work** — decode the Excel binary and apply the offline field
  treatment. It does **not** touch the DB: you feed it the lookup rows you fetched via the MCP.
  It needs only Python + an Excel reader locally (`python -m pip install xlrd openpyxl`).
- **The generic transaction rules are owned by the ayunit docs.** This skill does not restate
  them — it reads them live via the ayunit MCP doc tools (`search_docs` / `read_doc`) before it
  writes. The doc topics this skill relies on are listed under *Read first* below.

## Files in this skill

| File | Role |
|---|---|
| `parse_ubs.py` | Decode `.xls`/`.xlsx`, apply all field treatments, and (given MCP-gathered lookups) resolve assets, map accounts, lock-gate → `<file>.plan.json` + review table. Never writes, never touches the DB. |
| `mapping.md` | The UBS-Miami-specific field treatment (what the parser implements). Generic transaction rules live in the ayunit docs (read via the MCP) and are linked, not duplicated. |

Run the parser with any Python that has the Excel libs, pointing at the copy inside this skill:

```
python "${CLAUDE_PLUGIN_ROOT}/skills/ubs-miami/parse_ubs.py" "<file>"
```

(one-time `python -m pip install xlrd openpyxl`).

## Read first (from the ayunit MCP — the source of truth for the rules)

Before writing, pull the relevant transaction docs via the ayunit MCP (`search_docs` to find them,
`read_doc` to read them). The topics this skill depends on:

| Topic to read | Why |
|---|---|
| transaction / procedure · transaction / types | `AccountTransaction_Update` params, the sign table (always pass **absolute** values), the `VALIDATED`-requires-Asset hard error. |
| transaction / fixes | Universal write guardrails (duplicate pre-check, absolute values, AgentCheck). |
| checkeddate / usage | The lock contract — why lock-blocked rows are skipped, not forced. |
| `mapping.md` (in this skill) | The UBS→AccountTransaction mapping this skill applies (account map, Activity→Type, bond-vs-ETF price, OVERNIGHT sweep, Asset resolution order, Status/lock logic). |

## Inputs

A UBS Miami export path (`.xls` or `.xlsx`). Optionally one account to scope to. Echo the resolved
scope (file, accounts, date span) at the start. **Reply in the user's language (PT/EN).**

## The load cycle

### 1 — Parse the file & get the lookup queries
```
python "${CLAUDE_PLUGIN_ROOT}/skills/ubs-miami/parse_ubs.py" "<file>"     # .xls or .xlsx
```
With no lookups yet, the parser writes `<file>.lookups_needed.json` and prints **four SELECTs**
(AssetCustody, Global.v_Asset, ClientAccount, CheckedDate — all scoped to `UBS Miami`).

### 2 — Run those SELECTs via the MCP, save the rows
Run each printed query with `execute_select_query` (database `AgnesOrg00DB`). Collect the result
rows into one JSON keyed by the field names the parser asked for
(`assets_custody`, `assets_global`, `accounts`, `locks`) and save it as `<file>.lookups.json`.

### 3 — Build & review the plan
```
python "${CLAUDE_PLUGIN_ROOT}/skills/ubs-miami/parse_ubs.py" "<file>" --lookups "<file>.lookups.json"
```
Writes `<file>.plan.json` and prints the table + counts. **Show the user the summary**: the
**Write** vs **Ignore** split (each plan row carries `write: true/false`), counts per bucket
(`trade` / `cashflow` / `gl` / `unresolved` / `review` written PENDING; `lock_blocked` /
`unknown_account` / `zero_amount` ignored), the VALIDATED vs PENDING split, and the per-account cash
reconciliation. Call out everything that won't be written (the `write: false` rows).

### 4 — Triage the buckets (before writing anything)

**Core rule — never drop a movement that can be written.** A row that only fails mapping/validation
is still inserted, as `Status='PENDING'`, so it stays tracked in `Portfolio.AccountTransaction`
(losing it entirely is a critical audit failure). The parser marks each plan row `write: true/false`.

Written as **PENDING** (tracked, then fixed in place — *do not skip these*):
- **`unresolved`** — a trade whose CUSIP resolves in neither `Portfolio.v_AssetCustody` (Custody
  =`UBS Miami`) nor `Global.v_Asset` (e.g. *Blue Owl Tech Fin* `095924AB2`). Booked PENDING with
  `Asset` NULL (the proc's auto-match can later resolve it). Best practice: register via
  **asset-register** + add its `AssetCustody` mapping (`Portfolio.AssetCustody_Update`) *before* the
  load so it lands VALIDATED; otherwise re-validate after registering.
- **`review`** — `CANCEL BUY` reversals / any activity not in `ACTIVITY_MAP`. Booked PENDING with
  `TransactionType='UNKNOWN'`, carrying value + raw row, for a human to pair against the original buy
  or set the real type. Never auto-pair without the user.

**Ignored** (`write: false` — genuinely un-writable, reported only):
- **`lock_blocked`** — date ≤ the account's active CheckedDate. The procedure **rejects any write —
  PENDING included** — in a frozen, reconciled period. Correctly ignored; the row re-enters on a
  normal future load once the lock advances. Don't move the lock just to force them in (only for a
  deliberate, user-approved backfill via `Portfolio.CheckedDate_Update`).
- **`unknown_account`** — account number not in the UBS Miami book; no `ClientAccount` FK to attach
  to. Confirm out of scope, or register the account and re-run.
- **`zero_amount`** — a cash/GL row with a $0/blank amount. Nothing to book.

### 5 — Canary one insert (via the MCP)
Pick the first `write: true` VALIDATED row from `plan.json`. **Duplicate pre-check** it first
(`execute_select_query`), then write it with the MCP:
```
execute_procedure(database='AgnesOrg00DB', procedure='Portfolio.AccountTransaction_Update',
                  cmd='I', params=<that row's "params" from plan.json>)
```
SELECT it back and show the user before→after — confirm asset, signs, Price scale (bond ~99 /
ETF ~7), Value. **Pause for approval.**

### 6 — Batch the rest (via the MCP)
After approval, for each remaining **`write: true`** row — both `VALIDATED` and the `PENDING`
(`unresolved` / `UNKNOWN`) ones — dup-check, then `execute_procedure` (`cmd='I'`) with the row's
`params`. The PENDING rows keep the movement tracked; never drop them. Skip anything already present
(idempotent re-runs). Surface any failure (e.g. a RAISERROR) verbatim — don't bury it. The
`write: false` rows (`lock_blocked` / `unknown_account` / `zero_amount`) are reported, never inserted.

### 7 — Verify
```sql
SELECT ClientAccount, TransactionType, COUNT(*) n, SUM(Value) net_value
FROM Portfolio.v_AccountTransaction
WHERE Custody = 'UBS Miami' AND ClientAccount = '<account>'
  AND Date BETWEEN '<file start>' AND '<file end>'
GROUP BY ClientAccount, TransactionType ORDER BY TransactionType;
```
Add `Status` to the GROUP BY to see VALIDATED vs PENDING separately. Confirm the loaded count
matches the plan's `write: true` count, the net cash ties to the parser's reconciliation line, and
bond Prices are per-100 / ETF per-share. Report: **loaded VALIDATED / loaded PENDING / skipped-dup /
ignored (lock_blocked + unknown_account + zero_amount)**.

## Critical rules
- **Production. Confirm before writing** — canary first, batch only after the user approves.
- **One source of truth** — read schema/assets/accounts/locks live via the MCP; never hard-code
  them. Generic transaction rules come from the ayunit docs (read via the MCP), not restated here.
- **Never drop a writable movement.** Write every `write: true` row: `VALIDATED` *and* the `PENDING`
  ones (`unresolved` → Asset NULL; unmapped `review`/`CANCEL BUY` → `TransactionType='UNKNOWN'`).
  PENDING keeps the movement tracked in `AccountTransaction` for later fix-up — skipping it loses the
  audit trail. Only `lock_blocked`, `unknown_account`, and `zero_amount` are `write: false`.
- **Never write on/before an account's active CheckedDate** — the proc rejects *any* such write
  (PENDING included), so `lock_blocked` rows are correctly ignored; moving a lock is a separate,
  user-approved step.
- **Pass absolute `Quantity`/`Price`/`Value`** — the proc signs them from `TransactionType`.
- **Bond price is per-100, ETF/equity price is per-share** — auto-detected; sanity-check the canary.
- **Don't pass `AccountCurrency`/`AccountFx`** — computed internally; the wrapper 400s if you do.
  (The parser never emits them.)
- **Interest/GL rows always load**; **`CANCEL BUY` is booked PENDING/UNKNOWN, not auto-paired** —
  pair against the original buy or IGNORE it *with the user*, but the row is never dropped.
- **Re-runs are safe** thanks to the per-row dup pre-check, but still review the plan each time.

## When unsure
- **A new Activity value not in the map** → it's booked `PENDING` with `TransactionType='UNKNOWN'`
  (tracked, not lost); read the transaction/types doc via the MCP, decide the right TransactionType,
  add it to `ACTIVITY_MAP` in `parse_ubs.py`, then re-run to upgrade it from UNKNOWN to the real type.
- **A trade's Price scale looks off** (bond ~0.99 or ETF ~700) → the file's `Price` column was
  missing/odd so the scale fallback fired; check the asset's `SecurityType` and the row, fix, re-run.
- **Duplicate-looking rows already in the DB** → the dup pre-check skips them; if the user wants a
  reload anyway, investigate the existing pk first.
