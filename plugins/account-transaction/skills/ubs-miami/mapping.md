# UBS Miami activity → `Portfolio.AccountTransaction` — field treatments

The exact rules `parse_ubs.py` applies to turn one UBS Miami *Investment Activity* row (from a
`.xls` or `.xlsx` export) into a `Portfolio.AccountTransaction_Update @CMD='I'` payload. Every rule
was ground-truthed against the live `AgnesOrg00DB` (existing trades for the same assets in XP / MS,
the custody/account masters, and the CheckedDate locks).

> **Scope.** This documents only the **UBS-Miami-specific** mapping. The generic transaction
> semantics — sign conventions, Status flow, the `VALIDATED`-needs-Asset rule, validators — are
> owned by the ayunit transaction docs (read them live via the ayunit MCP `search_docs` /
> `read_doc`) and are linked, not restated, so there's a single source of truth.

## The source file

UBS Online Services → *Investment Activity* export — legacy BIFF `.xls` (read via `xlrd`) or
`.xlsx` (via `openpyxl`); both carry the same columns. The parser finds the header by locating the
`Account Number` row (the `.xls` has a "Filtered by …" banner above it; the per-account `.xlsx`
puts the header on row 0), then reads the rows below.

| Column | Meaning |
|---|---|
| `Account Number` | UBS account, e.g. `AE 22628` (note the space) |
| `Date` | single activity date (MM/DD/YYYY) — used as **both** `Date` and `SettlementDate` |
| `Activity` | `BOUGHT` / `SOLD` / `CALL REDEMPTION` / `CANCEL BUY` / `INTEREST` / `DEPOSIT` / `WITHDRAWAL` / `FEE CHARGE` |
| `Description` | free text (security name, or the cash-movement narrative) |
| `Symbol` | UBS ticker (mostly blank for bonds; e.g. `MMPUPG` for the sweep) |
| `Cusip` | the security identifier — **the asset matcher** |
| `Type` | `Investment` (securities) or `Cash` (flows / GL) |
| `Quantity` | signed face value (bonds) or share count (ETFs); blank for cash rows |
| `Price` | UBS *quoted* price — % of par for bonds (~99.7), per-share for ETFs (~7.2) |
| `Amount` | signed cash impact (the real money) |

## Fixed values (every row)

- `Custody = 'UBS Miami'` · `Currency = 'USD'` · all three accounts are **Offshore** (the proc
  forces `PriceExFee`, so we always send it).
- `Date = SettlementDate = ` the file `Date` (the activity report carries one date). *If a
  desk needs the true settlement offset, that's a per-row override — ask.*
- `RawTransaction` = the original row as JSON (audit trail).

## Account map (`Account Number` → `ClientAccount`)

**Derived live**, not hard-coded. `account_map_from_rows()` consumes `Global.v_ClientAccount WHERE
Custody='UBS Miami'` rows and indexes **both** the `Nickname` and the `ClientAccount` columns under
a normalised key (whitespace stripped, upper-cased). The file's `Account Number` equals the
`Nickname` (`AE 22628`), and the `ClientAccount` (`AE22628`) is the same modulo the space — both
collapse to the same key, so any account in the book resolves with **no code edit**. A label the
book genuinely lacks is flagged (`UNKNOWN account …`), never guessed. `ACCOUNT_OVERRIDES` (empty
by default) is the escape hatch for a file label the DB can't match.

The `accounts` rows come from the MCP (`execute_select_query` on `sql_accounts()`) and are handed
to the parser via `--lookups`. As of this build the custody has 7 accounts; the three in the
sample export:

| File `Account Number` (= Nickname) | `ClientAccount` | Friendly | Client |
|---|---|---|---|
| `AE 22628` | `AE22628` | STEN MFO | 82006 |
| `AE 22629` | `AE22629` | SGSF | 81006 |
| `AE 23928` | `AE23928` | SGSF NASSA | 81015 |

> The map is built from live `Global.v_ClientAccount` rows, so a newly-onboarded UBS Miami account
> resolves with no code edit. With no lookups supplied the parser only emits the queries to run
> (nothing is resolved or written until you feed the results back via `--lookups`).

## Activity → `TransactionType`

| UBS `Activity` | `TransactionType` | Bucket | Notes |
|---|---|---|---|
| `BOUGHT` | `BUY` | trade | |
| `SOLD` | `SELL` | trade | |
| `CALL REDEMPTION` | `SELL` | trade | early redemption at par (`Price`≈100, cash in, bond out) — a SELL |
| `INTEREST` | `GENERAL LEDGER RECEIPT` | gl | coupon/sweep interest; `GeneralLedgerType='INTEREST/DIVIDEND'` |
| `FEE CHARGE` | `GENERAL LEDGER DELIVERY` | gl | `GeneralLedgerType='FEE'` |
| `DEPOSIT` | `DEPOSIT` | cashflow | |
| `WITHDRAWAL` | `WITHDRAW` | cashflow | |
| `CANCEL BUY` | `UNKNOWN` | **review** | reversal of a prior buy — booked **PENDING/UNKNOWN** (never dropped); pair with the original or IGNORE **by hand** |

You always pass **absolute** `Quantity`/`Price`/`Value`; the proc applies the sign from
`TransactionType` (see the transaction/types doc via the ayunit MCP). Never pre-flip.

## Per-bucket field treatment

### trade (`BUY` / `SELL`)
- `CustodyIdentifier = Cusip`, `AssetCustody = Symbol` (or the description if no symbol).
- `Asset = AssetRelated =` resolved security (see *Asset resolution*). For BUY/SELL `AssetRelated = Asset`.
- `Quantity = ABS(file Quantity)` (face value for bonds; share count for ETFs).
- `ValueGross = Value = ABS(file Amount)` — the real cash. **No tax gap** on these offshore
  trades, so gross = net.
- `Price = PriceExFee = ` the **effective** price recomputed from cash, *not* the UBS quoted
  price (this is how XP/MS feeds store it — it folds in fees/accrued):
  - `raw = ABS(Amount) / ABS(Quantity)`
  - **scale**: bonds are quoted per-100, ETFs/equities per-share. Decide from the file's own
    `Price` column: if `Price/raw ≈ 100` → store `raw×100` (bond, ~99.7); if `≈ 1` → store
    `raw` (ETF, ~7.2). Fallback when `Price` is absent: `SecurityType='ETF'`/`AssetGroup` in
    (`Mutual Fund`,`Equity`) → per-share, else per-100.

### cashflow (`DEPOSIT` / `WITHDRAW`)
- `Asset = 'USD'`, `AssetRelated = NULL`, `Quantity = Value = ABS(Amount)`, `Price = 1`,
  `Obs = Description`.

### gl (`GENERAL LEDGER RECEIPT` / `DELIVERY`)
- `Asset = 'USD'`, `Quantity = Value = ABS(Amount)`, `Price = 1`,
  `GeneralLedgerDescription = Description`.
- **`GeneralLedgerType`**:
  - `FEE CHARGE` → `FEE`.
  - `INTEREST` on a **security** (coupon, resolvable CUSIP) → `INTEREST/DIVIDEND`, with
    `AssetRelated =` the paying bond.
  - `INTEREST` on the **UBS Insured Sweep Program** (`Symbol='MMPUPG'`, CUSIP `90499A981`, or a
    `SWEEP`/`OVERNIGHT` description) → **`OVERNIGHT`** with `AssetRelated = NULL` — this is cash
    overnight interest, not a coupon, and matches how XP/BTG remunerated-account interest is
    typed. Detected by the sweep's identifiers, **not** by "the asset didn't resolve" (an
    unregistered bond's coupon is still `INTEREST/DIVIDEND`).
- All still `VALIDATED` because `Asset='USD'` resolves (`AssetRelated` is not required).

> **GL / interest rows always load.** They are part of every UBS Miami import, not optional. A
> full load writes `trade` + `cashflow` + `gl`.

## Asset resolution (CUSIP → `Asset`)

`parse_ubs.py` resolves once, in bulk, **first hit wins**:

1. **`Portfolio.v_AssetCustody` (Custody = `UBS Miami`)** — the *custody-authoritative*
   `TickerCustody`/`TickerCustody2` → `Asset` map the position pipeline itself uses. This is
   where the firm explicitly records "this UBS identifier IS this asset", so it resolves cases
   the security master alone fumbles — e.g. BNP `F1R15XK51` is keyed here directly, and
   `HYGU LN Equity`'s key is clean (no stray `\xa0`). Matched CUSIP-*contains* (the ticker can
   carry whitespace; `TickerCustody2` sometimes holds the ISIN), finalised by exact compare.
2. **`Global.v_Asset` — exact CUSIP** — the security master (CUSIP-contains + Python exact, to
   tolerate the stray trailing chars some master rows carry).
3. **`Global.v_Asset` — CUSIP inside the ISIN** — UBS prints `F1R15XK51`; a master ISIN may be
   `USF1R15XK516`.

Whatever path resolves the asset, it's **enriched with `Global.v_Asset` metadata**
(`AssetGroup`/`SecurityType`) so the per-100/per-share price fallback still works, and the
AssetCustody `PriceFactor`/`PositionFactor` are carried — currently all `1.0`, so they don't
affect scaling; a value `≠ 1` is **flagged** (not silently applied) for review.

A CUSIP that resolves nowhere (e.g. `095924AB2`, *Blue Owl Technology Fin* — registered in
neither table) → the trade is **booked PENDING with `Asset` NULL** (never dropped — the movement
stays tracked in `AccountTransaction`), and reported. `VALIDATED` would raise without an Asset.
Registering it is a job for the **asset-register** skill; after that, re-run and it resolves
(ideally by also adding its `AssetCustody` mapping via `Portfolio.AssetCustody_Update`).

> The proc has its own auto-match too (`@AssetCustody`/`@CustodyIdentifier` across 9 columns).
> We resolve up-front anyway so the Status (VALIDATED vs PENDING) is decided *before* the write
> — `Status='VALIDATED'` without a resolvable Asset raises in the proc.

## Status, the `write` flag & the lock gate

**Guiding rule: never drop a movement that can be written** — losing it loses the audit trail. Every
plan row carries `write: true/false`. A row that only fails mapping/validation is persisted
`PENDING`, not skipped.

- `VALIDATED` (`write: true`) — the Asset resolves **and** the date clears the account's CheckedDate.
- `unresolved` → **PENDING** (`write: true`) — trade whose CUSIP isn't registered; `Asset` NULL.
- `review` → **PENDING / `TransactionType='UNKNOWN'`** (`write: true`) — `CANCEL BUY` or any activity
  not in `ACTIVITY_MAP`; carries value + raw for a human to pair / set the real type.
- **Lock gate** — read `Portfolio.v_CheckedDate WHERE Custody='UBS Miami' AND Activated=1`. The proc
  refuses **any** write (PENDING included) whose `Date`/`SettlementDate` ≤ the active lock. As of this
  build: `AE22628`/`AE22629` locked ≤ `2025-12-31`; **`AE23928` locked ≤ `2026-04-15`**. Rows
  on/before the lock are `lock_blocked` (`write: false`) — correctly **ignored** (frozen reconciled
  period; they re-enter on a future load once the lock advances). Lifting a lock is a user-approved
  `Portfolio.CheckedDate_Update`, never silent. (See the checkeddate/usage doc via the ayunit MCP.)

## Buckets the parser emits

Written (`write: true`): `trade` · `cashflow` · `gl` (VALIDATED), plus `unresolved` and `review`
(PENDING). Ignored (`write: false`, reported only — genuinely un-writable): `lock_blocked` (behind a
CheckedDate), `unknown_account` (no `ClientAccount` FK), `zero_amount` (nothing to book).
