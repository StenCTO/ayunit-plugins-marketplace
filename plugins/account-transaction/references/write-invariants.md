# Portfolio.AccountTransaction — universal write invariants

Authoritative reference for every write against `Portfolio.AccountTransaction`
via `Portfolio.AccountTransaction_Update`. Each leaf skill in
`account-transaction/skills/` cites this file rather than restating the rules.
Edit here first; skills MAY paraphrase but MUST NOT contradict.

Cross-references to the ayunit MCP docs — always the source of truth for
procedure shapes:
[`transaction/procedure`](ayunit://docs/transaction/procedure) ·
[`transaction/types`](ayunit://docs/transaction/types) ·
[`transaction/fixes`](ayunit://docs/transaction/fixes) ·
[`checkeddate/usage`](ayunit://docs/checkeddate/usage).

---

## The six rules

### 1. Always read from the view

Read `Portfolio.v_AccountTransaction`, never the base table. The view resolves
FK columns (`ClientAccount`, `Custody`, `Asset`, `AssetRelated`, `Currency`, …)
to their readable string form, which is what `AccountTransaction_Update`
expects on input.

### 2. SELECT-first-merge on every `@CMD='U'`

`@CMD='U'` overwrites **every** column from what you pass. Omitting a
populated field is data loss — the omitted column becomes NULL.

Contract per row:

1. `SELECT` the current row (all columns you'll reason about).
2. Build the params from **every populated column** you read.
3. Overlay only the fields you actually want to change.
4. Submit.

Applies identically to single-row `execute_procedure` and multi-row
`execute_batch` calls.

### 3. Drop `AccountCurrency` and `AccountFx` from every payload

Both are computed internally by the procedure. If either is present in the
params, the MCP wrapper rejects the whole call with a generic 400 before SQL
ever runs. Strip both keys after the SELECT-first-merge in rule 2, never after.

### 4. Preserve `RawTransaction` verbatim

`RawTransaction` is the original custody payload (nvarchar(max) JSON). It is
the audit trail for anything that touches the row later. On every `@CMD='U'`:
read the full string, pass it back untouched. Do not shrink, re-format, or
regenerate it, even for large JP / MS payloads.

### 5. Pass absolute values; the procedure applies the sign

Send `|Quantity|`, `|Price|`, `|PriceExFee|`, `|Value|`, `|ValueGross|`. The
procedure signs them from `TransactionType` per the table in
[`transaction/types`](ayunit://docs/transaction/types):

| TransactionType | Quantity | Price/PriceExFee | Value/ValueGross |
|---|---|---|---|
| SELL, ASSET DELIVERY | − | + | + |
| BUY, ASSET RECEIPT | + | + | − |
| GL RECEIPT | + | + | + |
| GL DELIVERY | − | + | − |
| WITHDRAW | − | 1 (forced) | − |
| DEPOSIT | + | 1 (forced) | + |

### 6. Lock-gate every write against `CheckedDate`

Read the active locks once per `(Account, Custody)` in scope:

```sql
SELECT Account, Custody, Date, Activated
FROM Portfolio.v_CheckedDate
WHERE Account IN (…) AND Activated = 1
ORDER BY Account, Date DESC;
```

- A row is **writable** only if `Date > lock AND SettlementDate > lock` (or no
  active lock for that `(Account, Custody)`).
- **Two `Activated=1` rows for one `(Account, Custody)` break the whole
  account** — the procedure's scalar subquery resolves the lock and raises
  SQL error 512 on more than one row, so *every* write on the account fails.
  Detect and skip the account; report the duplicate lock.
- Moving a lock backward is **audit-sensitive** and requires the recoil cycle
  (move lock → apply fix → re-run PortfolioCreator → analyst re-advances the
  lock). Never do it silently and never from a leaf skill. Surface as a
  paste-able `EXEC Portfolio.CheckedDate_Update …` for the analyst.

## Universal audit-trail rule

Every write sets `AgentCheck` with the skill's own tag so the next session
(and the audit's verification query) can distinguish paths. Format:

```
fix YYYY-MM-DD: <one-line what changed> [<TAG>]
```

Tags currently in use across the fleet: `[R6]` (compromissada-fix), `[AR]`
(assetrelated-fix), `[PR]` (pending-revalidate), `[PR-POS]`
(pending-position-repair), `[PQA]` (position-quantity-adjustment),
`[DUP-REVERT]` (duplicate-trade-reconcile Phase 1 IGNORE), `[ACF-<custody>]`
(assetcustody-fill — note this one lives only in the reply, not in the row,
since `Portfolio.AssetCustody` has no `AgentCheck` column).

Also set `Obs` when appropriate (e.g. `'automatic quantity adjustment'` /
`'automatic cash adjustment'` for `position-quantity-adjustment`).

## Status lifecycle (per `transaction/types`)

| Status | Enters `AccountPosition`? | Meaning |
|---|---|---|
| `PENDING` | no | raw loader row; may have `Asset=NULL` |
| `VALIDATED` | yes | passed validators; hard-errors if `Asset` unresolved |
| `UPDATED` | yes | human-corrected; counts like VALIDATED; skips the strict "Price required" check |
| `IGNORED` | no | excluded on purpose |

Only `VALIDATED` and `UPDATED` reach `AccountPosition` / `Share`.

## Auto-validators the procedure runs on `I` / `U` for asset trades

For `BUY`/`SELL`/`ASSET RECEIPT`/`ASSET DELIVERY` (per
[`transaction/procedure`](ayunit://docs/transaction/procedure)):

1. **Auto-match `Asset`** from `AssetCustody`/`CustodyIdentifier` via
   `Portfolio.v_AssetCustody` when `Asset` is empty.
2. **Auto-fill `Price`** from `AssetData.v_Price` only when `Price`, `Value`
   AND `ValueGross` are all 0/NULL and `Quantity ≠ 0` (come-cotas rows keep
   `ValueGross ≠ 0`, so this does NOT fire on them).
3. **Auto-fill `Quantity`** from `Value / Price` when `Quantity`/`Price`
   empty.
4. **Derive `Price`** as `ABS(Value / (Quantity × ContractSize))` whenever
   `Value` and `Quantity` are populated; `PriceExFee` falls back to `Price`
   only when passed as 0/NULL — so pass `PriceExFee` explicitly to keep NAV
   in place (relevant for come-cotas and any other case where the
   "computed" price would be wrong).

## Account-key format

Account keys are stored **per custody**:

- **BTG onshore** — zero-padded to 9 digits (`47067` → `'000047067'`).
- **XP onshore** — NOT zero-padded (`4789186` stays `'4789186'`).
- **Every other custody** — as stored.

Resolve once with `SELECT DISTINCT ClientAccount, Custody FROM
Portfolio.v_AccountTransaction WHERE ClientAccount LIKE '%<short>%'` and reuse
the exact stored string thereafter. Passing the wrong form silently touches
zero rows on `calculate_portfolio` / `execute_procedure`.

## What NEVER to write from an autonomous leaf

- **`AccountCurrency` or `AccountFx`** (rule 3).
- **A row on/before the account's active `CheckedDate`** (rule 6).
- **Anything on an account with duplicate active locks** (rule 6).
- **A CheckedDate move** — that's the analyst's recoil-cycle decision, always.

---

_Owner: Sten Capital · Update this file when the procedure shape or the write
contract changes, then bump `plugins/account-transaction/.claude-plugin/plugin.json`
`version` per repo `CLAUDE.md` §2._
