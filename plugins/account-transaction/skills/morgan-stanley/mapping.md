# Morgan Stanley activity → `Portfolio.AccountTransaction` — field treatments

The exact rules `parse_ms.py` applies to turn one Morgan Stanley "All Activity" row into a
`Portfolio.AccountTransaction_Update @CMD='I'` payload. Grounded in the live `AgnesOrg00DB` — the
custody/account masters, `Portfolio.v_AssetCustody`, the locks, **and the firm's existing
`Custody='MS'` transactions** (the official MS loader's conventions were read back to derive this).

> **Scope.** This documents only the **MS-specific** mapping. Generic transaction semantics — sign
> conventions, Status flow, the `VALIDATED`-needs-Asset rule, validators — are owned by
> [`ayunit://docs/transaction/*`](ayunit://docs/transaction/types) and linked, not restated.

## The source file

Morgan Stanley Online → export activity (`.xlsx`, sheet `AllActivity`). A banner block sits above
the header; the parser finds the header by locating the **`Activity Date`** row, then reads below.
Three shapes seen, all handled: a single-account file (account named in the banner
`Account Activity for <X> from …`, **no** `Account` column), and all-/multi-account files (with an
`Account` column). Descriptions are multi-line; the parser flattens them.

| Column | Meaning |
|---|---|
| `Activity Date` | posting date → **`SettlementDate`** |
| `Transaction Date` | trade date → **`Date`** |
| `Account` (optional) | e.g. `AAA - 4177`, `Consulting Group Advisor - 3869`; else taken from the banner |
| `Activity` | the movement type (drives TransactionType — table below) |
| `Description` | security name / narrative (rate, due date, confirmation #) |
| `Symbol`, `Cusip` | `Cusip` is the asset matcher |
| `Quantity`, `Price($)`, `Amount($)` | signed; `Amount($)` is the real cash impact |

## Fixed values

- `Custody = 'MS'` · `Currency = 'USD'` · the MS accounts are **Offshore** (proc forces `PriceExFee`).
- **`Date` = `Transaction Date`**, **`SettlementDate` = `Activity Date`** (falls back to the other if one is blank).
- `RawTransaction` = the original row as JSON.

## Account map (`Account` label → `ClientAccount`)

**Derived live** from `Global.v_ClientAccount WHERE Custody='MS'`, indexing both `ClientAccount`
and `Nickname` under a whitespace-/case-normalised key. The file's label is sometimes the Nickname
(`AAA - 4177` → `711-024177-212`), sometimes equal to the ClientAccount (`AAA - 0990`). A label the
book doesn't have → flagged `unknown_account` (e.g. personal **CashPlus/checking** accounts that
aren't portfolio accounts) — reported, never guessed. `ACCOUNT_OVERRIDES` is the escape hatch.

> The MS book has a known duplicate Nickname (`711-029792` → two ClientAccounts); the parser warns
> on the collision. It doesn't affect the sample files.

## Activity → `TransactionType` (mirrors the firm's MS loader)

| MS `Activity` | `TransactionType` | `GeneralLedgerType` | Bucket |
|---|---|---|---|
| `Bought`, `Contribution`, `Subscription`, `Dividend Reinvestment` | `BUY` | — | trade |
| `Auto Bank Product Deposit`, `Bank Product Deposit` | `BUY` | — | trade (MSBNA sweep IN — Auto or manual) |
| `Sold`, `Redemption` | `SELL` | — | trade (Redemption = at par) |
| `Bank Product Withdrawal` | `SELL` | — | trade (MSBNA sweep OUT) |
| `Transfer into Account`, `Exchange In`, `Exchange Received In` | `ASSET RECEIPT` | — | trade (in-kind; **no cash leg**) |
| `Transfer out of Account`, `Exchange Out`, `Exchange Deliver Out` | `ASSET DELIVERY` | — | trade (in-kind; **no cash leg**) |
| `Interest Income`, `Interest`, `Dividend`, `Qualified Dividend` | `GENERAL LEDGER RECEIPT` | `INTEREST/DIVIDEND` | gl |
| `Interest Income` on the **MSBNA Preferred Savings sweep** | `GENERAL LEDGER RECEIPT` | `OVERNIGHT` | gl |
| `Refund`, `Miscellaneous Income` | `GENERAL LEDGER RECEIPT` | `OTHER` | gl |
| `Service Fee`, `Account Fee` | `GENERAL LEDGER DELIVERY` | `FEE` | gl |
| `Write Off`, `Margin Interest Charged`, `Interest Income-Adj`, plus any unmapped activity containing `"Interest"` | `GENERAL LEDGER DELIVERY` if Amount<0 else `GENERAL LEDGER RECEIPT` | `INTEREST/DIVIDEND` (`Interest` family) or `OTHER` (`Write Off`) | gl (direction from the Amount sign) |
| `Tax Withholding` | `WITHDRAW` | `TAXES` | cashflow |
| `Funds Paid`, `Funds Disbursed` | `WITHDRAW` | — | cashflow |
| `CASH TRANSFER`, `Funds Transferred`, `Funds Received`, `Debit Card`, `ATM Withdrawal`, `Online Transfer`, `Automated Payment`, `Automatic Deposit`, `Service Fee Adj`, `FX Cash Withdrawal` | `WITHDRAW` if Amount<0 else `DEPOSIT` | — | cashflow (direction from the Amount sign — sign is more reliable than label for these) |
| Any activity starting with `Pending …` (`Pending Card Trans`, `Pending Cash`) | — | — | **skipped** (`Status='IGNORED'`, `write:false`) — MS-side unconfirmed placeholders; they reappear once settled |
| `Zelle Payment`, `Sold - Adjusted` | — | — | **review** (ambiguous; needs human triage) |

Pass **absolute** `Quantity`/`Price`/`Value`; the proc applies the sign from `TransactionType`
(see `ayunit://docs/transaction/types`). The review bucket is the MS analogue of ubs-miami's
`CANCEL BUY`: rather than drop it, we **book it `PENDING` with `TransactionType='UNKNOWN'`** (the
canonical not-yet-mapped state — the official MS loader does the same) so the movement stays tracked
for a human to set the real type.

## Per-bucket field treatment

### trade (`BUY` / `SELL` / `ASSET RECEIPT` / `ASSET DELIVERY`)
- `CustodyIdentifier = Cusip`, `AssetCustody = Symbol` (or description), `Asset = AssetRelated =` resolved security.
- `Quantity = ABS(Quantity)`; `ValueGross = Value = ABS(Amount)`.
- `Price = PriceExFee =` effective price from cash, scaled per the file's own `Price` (bonds/CLOs
  per-100 ~99.7; ETFs/equities per-share). `Redemption` lands at `Price = 100`. Transfers have a
  `Price($)` of 0 in the file, so the scale comes from the resolved asset's `AssetGroup` (CLOs → per-100).
- **`ASSET RECEIPT` / `ASSET DELIVERY` carry NO cash leg.** Same field treatment as a BUY/SELL, but
  the position pipeline does **not** generate a currency/cash movement for these types (it's a pure
  custody/in-kind move) — `Value`/`ValueGross` are the (signed) market value, used only for the
  asset's accounting & performance (cost basis), not cash. See `ayunit://docs/portfolio-creator`
  (§ "do not generate this cash side") and `ayunit://docs/backoffice/decision-tree` (asset transfer
  = no cash leg). So an in-kind transfer raises the position without draining USD.

### cashflow (`DEPOSIT` / `WITHDRAW`)
- `Asset = 'USD'`, `Quantity = Value = ABS(Amount)`, `Price = 1`, `Obs = Description`.
- `Tax Withholding` is a `WITHDRAW` that also carries `GeneralLedgerType = 'TAXES'` and, when the
  taxed security resolves, `AssetRelated`.

### gl (`GENERAL LEDGER RECEIPT` / `DELIVERY`)
- `Asset = 'USD'`, `Quantity = Value = ABS(Amount)`, `Price = 1`, `GeneralLedgerDescription = Description`.
- `GeneralLedgerType` per the table; `AssetRelated =` the paying security (from `Cusip`), or NULL
  for the sweep / fees / unresolved.
- A **zero/blank-amount** cash or GL row (e.g. a $0 fee) is routed to `review` — nothing to book.

## Asset resolution (CUSIP → `Asset`)

Same order as ubs-miami, first hit wins: **1)** `Portfolio.v_AssetCustody` (Custody=`MS`,
`TickerCustody`/`TickerCustody2`), **2)** `Global.v_Asset` exact CUSIP, **3)** `Global.v_Asset`
CUSIP-in-ISIN (e.g. SocGen `F8500RAB8` → ISIN match). Enriched with `AssetGroup`/`SecurityType`
for the per-100/per-share fallback; `PriceFactor`/`PositionFactor` carried (flagged if ≠1).
A CUSIP that resolves nowhere → `unresolved` (PENDING) — register via **asset-register** + add its
`AssetCustody` mapping, then re-run.

## Status & buckets

Every plan row carries `write: true/false`. **Guiding rule: never drop a movement that can be
written** — losing it loses the audit trail. A row that only fails mapping/validation is persisted
`PENDING` (a tracked, fixable state), not skipped.

**Written (`write: true`):**
- `VALIDATED` — (trade) the Asset resolves, the account maps, and the date clears the lock; or
  (cash/gl with a non-zero amount) the account maps and the date clears the lock.
- `unresolved` → **PENDING** — trade whose CUSIP isn't registered; inserted with `Asset` NULL
  (proc auto-match / later registration resolves it). `VALIDATED` without an Asset would `RAISERROR`.
- `review` → **PENDING / `TransactionType='UNKNOWN'`** — activity not in `ACTIVITY_MAP`; carries the
  value + raw row for a human to set the real type.

**Ignored (`write: false`) — genuinely un-writable, reported only:**
- `lock_blocked` — `Date` **or** `SettlementDate` ≤ the account's active `Portfolio.v_CheckedDate`.
  `AccountTransaction_Update` rejects *any* write here, PENDING included — the period is frozen and
  reconciled. Correct to ignore; the row re-enters on a future load once the lock advances. Locks are
  moved only for a deliberate, user-approved backfill (`Portfolio.CheckedDate_Update`) — see
  `ayunit://docs/checkeddate/usage`.
- `unknown_account` — label not in the MS book; no `ClientAccount` FK to attach the row to.
- `zero_amount` — cash/GL row with a $0/blank amount; nothing to book.
