---
name: morgan-stanley
description: Use when the user wants to load a Morgan Stanley custody activity export (the "All Activity" .xlsx) into Portfolio.AccountTransaction — parse the file, apply every field treatment (account map, Activity→TransactionType, CUSIP→Asset resolution, bond per-100 vs ETF per-share price, sign handling, OVERNIGHT sweep interest, TAXES/FEE/OTHER GL types), lock-gate against CheckedDate, then insert the trades and cash/GL movements via Portfolio.AccountTransaction_Update @CMD='I'. A repeatable custody loader, run per export file. Sibling of the ubs-miami skill.
---

# Load Morgan Stanley activity into `Portfolio.AccountTransaction`

You are the orchestrator that turns a **Morgan Stanley "All Activity" export** (`.xlsx`, sheet
`AllActivity`) into validated `Portfolio.AccountTransaction` rows. Custody is `MS`; accounts are
USD / offshore. A full load covers the **trades** (Bought / Sold / Redemption) **and** the cash/GL
movements (interest, dividends, fees, taxes, external funds in/out). This is the **sibling of the
`ubs-miami` skill** — same architecture, guardrails, and scripts, adapted to the MS feed.

The MS feed is messier than UBS: it mixes investment activity with internal bank-sweep movements
and personal banking on CashPlus/checking accounts. Most of these now map cleanly — the MSBNA
sweep books as `BUY`/`SELL` of CUSIP `99YFH93X0`; debit card / ATM / online transfers / fee
adjustments are routed to the **sign-directed cashflow bucket** (the Amount sign decides
`WITHDRAW` vs `DEPOSIT`, since the label alone often doesn't). What remains genuinely ambiguous
(Zelle, Sold - Adjusted) is still **booked as `PENDING`
/ `UNKNOWN`**, never dropped, so every movement stays tracked for a human to triage. The only
rows never written are the genuinely un-writable ones (lock-blocked, unknown account, zero amount).

## Single source of truth & portability — read this first

- **The Ayunit backend / live DB is the one source of truth.** No hard-coded schema, account maps,
  or asset identifiers — read live. Transaction conventions are owned by `ayunit://docs/transaction/*`.
- **All DB access via the ayunit MCP tools** — `execute_select_query` (reads, dedup, verify) and
  `execute_procedure` (`Portfolio.AccountTransaction_Update`, `cmd='I'`). Only primary path.
- **`parse_ms.py` does only LOCAL work** (decode the Excel + offline field treatment). In the
  normal path it does NOT touch the DB — you feed it the lookup rows you fetched via the MCP
  (`--lookups`). So the folder works in any VS Code with the ayunit MCP connected — no `.env`, no venv.
- A REST fallback (`parse_ms.py --rest`, `write_inserts.py`) hits the **same** backend via the same
  allowlisted proc, for running inside this repo. Prefer the MCP path.

## Files in this folder

| File | Role |
|---|---|
| [`parse_ms.py`](parse_ms.py) | **Local only.** Decode `.xlsx`, apply all field treatments, and (given MCP-gathered lookups) resolve assets, map accounts, lock-gate → `<file>.plan.json` + review table. Never writes. |
| [`write_inserts.py`](write_inserts.py) | **Fallback batch writer** (REST, `.env`); custody-agnostic. Primary write path is the MCP `execute_procedure`. Dry-run default; `--canary`, `--account`, `--bucket`, per-row dup pre-check. |
| [`mapping.md`](mapping.md) | The MS-specific field treatment. Generic rules link to `ayunit://docs/transaction/*`. |

Run with any Python that has `openpyxl` (`python -m pip install openpyxl`): `python "${CLAUDE_PLUGIN_ROOT}/skills/morgan-stanley/parse_ms.py" …`.
Inside this repo you can use `../../../../.venv/Scripts/python.exe`.

## Read first

| Resource | Why |
|---|---|
| [`mapping.md`](mapping.md) | The MS→AccountTransaction mapping (account map, Activity→Type incl. the review bucket, bond-vs-ETF price, OVERNIGHT sweep, Asset resolution, Status/lock logic). |
| [`ayunit://docs/transaction/procedure`](ayunit://docs/transaction/procedure) · [`types`](ayunit://docs/transaction/types) | proc params, sign table (pass **absolute** values), `VALIDATED`-needs-Asset. |
| [`ayunit://docs/transaction/fixes`](ayunit://docs/transaction/fixes) · [`checkeddate/usage`](ayunit://docs/checkeddate/usage) | write guardrails; the lock contract. |

## Inputs

An MS `.xlsx` export. **Strongly recommend scoping to one account** (`--account`) — the all-accounts
export carries 30+ accounts, many of them checking/CashPlus accounts not in the book. Echo the
resolved scope (file, accounts, date span). **Reply in the user's language (PT/EN).**

## The load cycle

### 1 — Parse & get the lookup queries
```
python "${CLAUDE_PLUGIN_ROOT}/skills/morgan-stanley/parse_ms.py" "<file>.xlsx"
```
Writes `<file>.lookups_needed.json` and prints four SELECTs (AssetCustody, Global.v_Asset,
ClientAccount, CheckedDate — scoped to `MS`).

### 2 — Run those SELECTs via the MCP, save the rows
Run each with `execute_select_query` (DB `AgnesOrg00DB`); save the result rows into one JSON keyed
`assets_custody` / `assets_global` / `accounts` / `locks` as `<file>.lookups.json`.
*(In-repo shortcut: skip 1–2 and use `python "${CLAUDE_PLUGIN_ROOT}/skills/morgan-stanley/parse_ms.py" "<file>.xlsx" --rest`.)*

### 3 — Build & review the plan
```
python "${CLAUDE_PLUGIN_ROOT}/skills/morgan-stanley/parse_ms.py" "<file>.xlsx" --lookups "<file>.lookups.json"   # add --account NAME to scope
```
Writes `<file>.plan.json` + a table. **Show the user** the bucket/status counts and the per-account
cash reconciliation, and call out everything that won't be written.

### 4 — Triage the buckets (before writing)

**Core rule — never drop a movement that can be written.** A row that only fails mapping/validation
is still inserted, as `Status='PENDING'`, so it stays tracked in `Portfolio.AccountTransaction`
(losing it entirely is a critical audit failure). The parser marks each plan row `write: true/false`.

Written as **PENDING** (tracked, then fixed in place — *do not skip these*):
- **`unresolved`** — a trade whose CUSIP isn't in `Portfolio.v_AssetCustody` nor `Global.v_Asset`.
  Inserted PENDING with `Asset` NULL (the proc's auto-match can later resolve it). Best practice:
  register via **asset-register** + add its `AssetCustody` mapping *before* the load so it lands
  VALIDATED; otherwise it's booked PENDING and you re-validate after registering.
- **`review`** — an activity left unmapped (today: `Zelle Payment`, `Sold - Adjusted`). Inserted
  PENDING with `TransactionType='UNKNOWN'`, carrying the value + raw row, for a human to set the
  real type. *(In-kind transfers/exchanges auto-map to `ASSET RECEIPT`/`ASSET DELIVERY` — no cash
  leg; MSBNA sweeps (Auto and manual) auto-book as `BUY`/`SELL` of the sweep CUSIP; debit card /
  ATM / online transfers / `Service Fee Adj` are sign-directed cashflow; `Qualified Dividend` is
  GL RECEIPT; `Write Off` / `Margin Interest Charged` / `Interest Income-Adj` and other unmapped
  "Interest"-containing activities are sign-directed GL — so none of those land here.)*
- **`pending_skip`** — any activity whose label starts with `Pending ` (e.g., `Pending Card Trans`,
  `Pending Cash`) is **skipped entirely** (`write:false`, would persist as `Status='IGNORED'` if
  ever forced through). These are MS-side unconfirmed placeholders that get re-inserted or deleted
  once the underlying transaction settles, so loading them would just churn.

**Ignored** (`write: false` — genuinely un-writable, reported only):
- **`lock_blocked`** — `Date` or `SettlementDate` ≤ the account's active CheckedDate. The procedure
  **rejects any write — PENDING included** — in a frozen, reconciled period. These are correctly
  ignored; the row re-enters on a normal future load once the lock advances. Do **not** move the
  lock just to force them in (only do so for a deliberate, user-approved backfill).
- **`unknown_account`** — label not in the MS book (often a checking/CashPlus account). No
  `ClientAccount` FK to attach the row to. Confirm out of scope, or register the account and re-run.
- **`zero_amount`** — a cash/GL row with a $0/blank amount. Nothing to book.

### 5 — Canary one insert (via the MCP)
Pick the first `write: true` VALIDATED row; **duplicate pre-check** it (`execute_select_query`), then
write with `execute_procedure(database='AgnesOrg00DB',
procedure='Portfolio.AccountTransaction_Update', cmd='I', params=<row's "params">)`. SELECT it back,
show before→after (asset, signs, Price scale, Value). **Pause for approval.**
*(In-repo: `python "${CLAUDE_PLUGIN_ROOT}/skills/morgan-stanley/write_inserts.py" "<file>.plan.json" --account <ClientAccount> --canary --confirm`.)*

### 6 — Batch the rest (via the MCP)
For each remaining **`write: true`** row — both `VALIDATED` and the `PENDING` (`unresolved` /
`UNKNOWN`) ones — dup-check, then `execute_procedure` (`cmd='I'`). The PENDING rows keep the movement
tracked; never drop them. Skip rows already present (idempotent). Surface any failure verbatim.
**Do this per account** (`--account`) so each account is a clean, verifiable unit. The `write: false`
rows (`lock_blocked` / `unknown_account` / `zero_amount`) are reported but never inserted.
*(In-repo: `python "${CLAUDE_PLUGIN_ROOT}/skills/morgan-stanley/write_inserts.py" "<file>.plan.json" --account <ClientAccount> --confirm`.)*

### 7 — Verify
```sql
SELECT TransactionType, COUNT(*) n, SUM(Value) net_value
FROM Portfolio.v_AccountTransaction
WHERE Custody='MS' AND ClientAccount='<account>'
  AND Date BETWEEN '<start>' AND '<end>'
GROUP BY TransactionType ORDER BY TransactionType;
```
Add `Status` to the GROUP BY when you want to see VALIDATED vs PENDING separately. Confirm the loaded
count matches the plan's `write: true` count and the net ties to the parser's reconciliation line.
Report: **loaded VALIDATED / loaded PENDING / skipped-dup / ignored (lock_blocked + unknown_account +
zero_amount)**.

## Critical rules
- **Production. Confirm before writing** — canary first, batch per account after approval.
- **One source of truth** — read schema/assets/accounts/locks live via the MCP; generic rules from
  `ayunit://docs/transaction/*`.
- **Never drop a writable movement.** Write every `write: true` row: `VALIDATED` *and* the `PENDING`
  ones (`unresolved` → Asset NULL; unmapped `review` → `TransactionType='UNKNOWN'`). PENDING keeps the
  movement tracked in `AccountTransaction` for later fix-up — skipping it loses the audit trail.
  Only `lock_blocked`, `unknown_account`, and `zero_amount` are `write: false` (un-writable, reported).
- **Never write on/before an account's active CheckedDate.**
- **Pass absolute `Quantity`/`Price`/`Value`** — the proc signs them.
- **Bond price per-100, ETF/equity per-share** (auto-detected; sanity-check the canary).
- **Don't pass `AccountCurrency`/`AccountFx`** (computed internally). The parser never emits them.
- **Don't auto-book the `review` bucket** (transfers / sweeps / personal banking) — pair or decide with the user.
- **Re-runs are safe** (per-row dup pre-check keyed incl. `AssetRelated`), but review the plan each run.
- A correct **USD cash position also needs the account's inception/opening cash seed to be right** —
  that's a separate step from this transaction load (see the ubs-miami AE23928 lesson).

## When unsure
- **A new Activity not in the map** → it lands in `review`; read `ayunit://docs/transaction/types`,
  decide the type, add it to `ACTIVITY_MAP` in `parse_ms.py`, re-run.
- **A Price scale looks off** (bond ~0.99 or ETF ~700) → the file `Price` was missing/odd and the
  fallback fired; check `SecurityType` and the row, fix, re-run.
- **Transfers / sweeps the user wants booked** → confirm the exact convention (in-kind ASSET
  RECEIPT/DELIVERY; whether internal sweeps post at all), then extend `ACTIVITY_MAP` and re-run.
