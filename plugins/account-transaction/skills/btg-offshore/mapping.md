# BTG Offshore (Cayman) -> `Portfolio.AccountTransaction` mapping contract

The single source of truth for how a raw BTG Offshore operation becomes a
`Portfolio.AccountTransaction` row. **If the code and this document ever disagree, fix both
together.** In the Cowork / ayunit-MCP port the logic lives in ONE local-only file:

- **`scripts/parse_btg.py`** — reads the `get_btg_offshore_trades` feed + the lookups you fetched via
  the MCP, applies the BTG-offshore **format treatment** (per-section handlers, sign/price rules),
  enforces **system coherence** (valid type/GL/status), the **CheckedDate gate**, and **duplicate
  detection**, then emits the `execute_batch` items. It never touches the DB.

Custody / Source / Broker label is **`BTG Cayman`** (separates offshore from the onshore `BTG`).
Currency is **USD**. Rows are inserted via `Portfolio.AccountTransaction_Update @CMD='I'`, committed
one account at a time through the ayunit **`execute_batch`** tool (atomic, all-or-nothing per account).

---

## 1. Target structure — `Portfolio.AccountTransaction_Update` (`@CMD='I'`)

The proc resolves the foreign keys itself: `Asset`/`AssetRelated` -> `fk_AssetID` by exact match on
`Global.Asset.Asset` (with an auto-match fallback over `AssetCustody`/`CustodyIdentifier` against
Isin/Cusip/BbgCode/...); `TransactionType`/`GeneralLedgerType`/`Currency`/`Broker`/`Custody` against
their `Global.*` lookups. So we pass the **resolved canonical asset name** in `Asset`, not a raw id.
Batch/procedure `params` use **bare names** (no `@`, no `CMD` key — `cmd` is the item field).

| Parameter | Meaning / rule |
|---|---|
| `cmd` | `'I'` (insert) — the execute_batch item field, not a param |
| `Date` | trade date (`YYYY-MM-DD`) — required |
| `SettlementDate` | settlement date — required, **must be >= `Date`** (else set = `Date`) |
| `ClientAccount` | BTG account number — required |
| `Broker` | `BTG Cayman` |
| `Custody` | `BTG Cayman` — required |
| `TransactionType` | one of the valid types below — required |
| `GeneralLedgerType` | set only for `GENERAL LEDGER *` rows (else omitted) |
| `GeneralLedgerDescription` | free text for GL rows |
| `Currency` | `USD` — required |
| `AssetCustody` | raw custody id: ISIN when present, else the BTG `assetId` |
| `CustodyIdentifier` | the BTG `assetId` (drives the proc's asset auto-matching) |
| `Asset` | **resolved canonical** asset (or `USD` for cash); empty -> proc auto-match (PENDING if it fails) |
| `AssetRelated` | **must equal `Asset`** for BUY/SELL/ASSET RECEIPT/ASSET DELIVERY; the paying security for income GL |
| `Quantity` | **absolute**; required when VALIDATED |
| `Price` | **absolute** custody price; required when VALIDATED (see §4) |
| `ValueGross`, `Value` | **absolute** cash impact (USD) — `Value` required |
| `Status` | `VALIDATED` or `PENDING` (see §5) |
| `Obs` | human-readable description |
| `RawTransaction` | JSON of the source record (audit trail; carries the `tradeId` for dedup) |
| `AccountCurrency`, `AccountFx` | **never passed** — the proc computes them (the MCP wrapper rejects them) |

**Valid `TransactionType`** (`Global.TransactionType`): `BUY`, `SELL`, `ASSET RECEIPT`,
`ASSET DELIVERY`, `DEPOSIT`, `WITHDRAW`, `GENERAL LEDGER RECEIPT`, `GENERAL LEDGER DELIVERY`,
`UNKNOWN`.

**Valid `GeneralLedgerType`** (`Global.GeneralLedgerType`): `INTEREST/DIVIDEND`, `FEE`, `TAXES`,
`OVERNIGHT`, `OTHER`, `FX SPOT`, `Forward Maturity` (NDF/FX-forward maturity settlement).

> **The proc signs everything.** Pass absolute magnitudes; `AccountTransaction_Update` applies the
> sign from `TransactionType` (BUY -> +Qty/-Value, SELL -> -Qty/+Value, GL DELIVERY/WITHDRAW negative,
> GL RECEIPT/DEPOSIT positive) and derives `Price = ABS(Value / (Quantity * ContractSize))` itself
> (Validator 1.4). A VALIDATED row hard-requires a resolvable `Asset` + `Quantity` + `Price`, else it
> RAISERRORs — that is why an unresolved row must be `PENDING`.

---

## 2. Source feed — what `get_btg_offshore_trades` returns

The MCP tool returns `{"trades": [ entry, ... ], "summary": {...}}`. Each **entry** is one
`(account, operation-day)` snapshot and carries `accountNumber`, `positionDate` (BTG's business
date), `_operation_date` (the calendar day the wrapper queried), and one list per **section**. Each
section is a list of blocks, each block a `{"trade": [ record, ... ]}`. Sections seen:
`cash`, `equitiesOptions`, `fixedIncome`, `equities`, `funds`, `timeDeposit`, `forwards`, `futures`,
and the **non-bookable** `payableReceivable` (the receivable/payable balance — never booked).
All numeric fields arrive as **strings** (coerce with `_f`). `funds` records carry **no `accountId`**
— the account is taken from the entry's `accountNumber`.

> **Weekend / holiday repeats.** The tool walks every calendar day, but BTG re-reports the last
> business day's snapshot on non-business days (Sat/Sun carry the Friday `positionDate`). Keep only
> entries where **`_operation_date == positionDate`**; the `(tradeId, |value|)` dedup (§7) then catches
> any remaining cross-day echoes (a T+1 trade shown on two days). Two payload field quirks to code to:
> futures carry `codTitulo` (doc says `codTitle`), equities carry `positionDate` (doc says
> `operationDate`); the wrapper's `summary.total_trades` is unreliable — ignore it.

---

## 3. Where each row comes from — no double-counting

Every security trade appears **twice** in the feed: once in its detail section (qty / price / ids)
and once in the `cash` ledger as a `SECURITY` cash leg. The split below books each economic event once.

| AccountTransaction row | Source | Notes |
|---|---|---|
| **BUY / SELL** (securities) | detail sections: equitiesOptions, fixedIncome, equities, timeDeposit, funds, futures | direction from the cash `value` sign, confirmed by the trade label (Sale/Purchase/New/Maturity/Redemption); carries qty/price/asset |
| **OTM option expiry** | equitiesOptions (`tradeDescription`~`Expiry`, `value=0`) | worthless close at **Price 0 / Value 0**. BTG sends the expiry quantity **unsigned** with no side, so the parser tags the row and resolves the side from the **held position** — **BUY** to cover a net short, **SELL** to close a net long. Held = inception seed (`AccountPosition` at the CheckedDate) **+** net of post-inception legs (DB union this batch). Zero/unknown held -> PENDING. |
| **Equity dividend** | equities (`description`=`Cash Dividend`, `quantity=0`) | -> `GENERAL LEDGER RECEIPT`/`DELIVERY` `INTEREST/DIVIDEND`; AssetRelated = the equity (resolved by ISIN/id) |
| **Bond coupon / interest** | fixedIncome (`description`/`eventType`~`Coupon`) | bond *income*, not a sale -> GL `INTEREST/DIVIDEND`, Asset `USD`, AssetRelated = the bond, Quantity = Value, Price 1. (A bond **`Maturity of Security`** is NOT a coupon -> it is a SELL of the full face that closes the position at par.) |
| **Future** | futures | **BUY/SELL the quantity traded @ Value 0 / Price 0** — a future exchanges *margin*, not notional, so the **trade leg** has **no cash impact**. Direction from the quantity sign / Sale-Purchase label; the feed `value` is ignored for cash. A future's cash comes from the **cash ledger** in two forms: its **commission** (`transferType=CLIENT_FEE_FUT`, "... Commission") -> a **FEE** GL, and its **realized P&L** (`transferType=REALIZED_PL`, variation-margin settlement) -> a `GENERAL LEDGER RECEIPT`/`DELIVERY` of type `INTEREST/DIVIDEND` with `AssetRelated` = the **futures asset** (linked `tradeId` -> the futures-section `assetId`). The realized P&L is VALIDATED once that future is registered, else PENDING (linked on re-run) — never dropped. |
| **NDF / FX-forward** (position legs) | forwards | **always Qty 1 @ Value 0 / Price 0**; side fixed by lifecycle, NOT the USD-leg sign: `New` -> **BUY 1**, `Maturity` -> **SELL 1** (mirrors the custody snapshot's open NDF at Qty +1). Legs get phase-suffixed `tradeId` (`:NDF_NEW`/`:NDF_MAT`). **No GL/cash leg is produced from forwards** — see the next row. |
| **NDF net cash settlement** | **cash ledger** (`productType=FXNDF`, `transferType=PRINCIPAL`) | the only cash an NDF produces (the net PnL at maturity) -> `GENERAL LEDGER RECEIPT`/`DELIVERY` of GL type **`Forward Maturity`**, Asset `USD`, AssetRelated `NDF<tradeId>`, Value = the cash-ledger `value`, **dated on the settlement date**. VALIDATED. |
| **DEPOSIT / WITHDRAW** | cash ledger | `Deposit ...` / `Cash Withdrawal ...` (CustomerTransfer / PRINCIPAL), **only when not a dividend/interest** (caught first) |
| **GENERAL LEDGER RECEIPT / DELIVERY** (income) | cash ledger | **Dividends, fund distributions, security interest** -> `INTEREST/DIVIDEND`, Asset `USD`, AssetRelated = the **paying security**, Quantity = Value, Price 1. Detection: `transferType=DIVIDEND`, `productType=CA`, or `DIVIDEND`/`DISTRIBUTION` anywhere in the description (catches a fund's `"Cash Dividend - <fund> - Trade Id ..."` and `"... - <fund> - Cash Distribution"` via `CustomerTransfer`). Payer resolved from the description: exact `Global.Asset` Description, else best distinctive-token overlap among assets the **account holds** (e.g. `Pearl Diver Floating Rate Global` -> `PEARL DIVER`); unresolved -> `PENDING`. `Daily Interest` stays account-level overnight cash (`OVERNIGHT`, no AssetRelated). Also: fees (`FEE`/`CUSTODY_FEE`/`CLIENT_FEE_FUT`/"Commission"), taxes (`TAXES`). |

> **Equities carry `amount` (cash, with fee) and `baseValue` (ex-fee), not a `value` field** —
> `Value` comes from `amount`, `ValueGross` from `baseValue`, `BrokerageFee` is their difference.

> **Asset resolution** is done against the injected lookups: `Portfolio.v_AssetCustody` by
> `TickerCustody` (the BTG `assetId`), then `Global.v_Asset` by ISIN, then exact `Global.Asset`
> Description (for no-ISIN instruments such as listed options).

**Excluded from the cash ledger** (booked elsewhere or not real cash):
- `transferType == 'SECURITY'` -> the cash leg of a security trade (booked from detail)
- `productType == 'StructuredFlows'` -> Time-Deposit principal/interest (booked from TD detail)
- `Margin Call` / `Margin Release` -> internal margin transfers (net to zero)
- `Capital Call` / `Capital Return` / `Income` -> fund cash legs (booked from funds detail)

---

## 4. Direction, quantity and price conventions

- **Pass absolute magnitudes.** `Quantity`, `Price`, `ValueGross`, `Value` are absolute; the proc
  signs them from `TransactionType`. Do **not** pre-sign.
- **Direction** (which TransactionType) is the cash `value` sign — `value < 0` (out) -> **BUY**,
  `value > 0` (in) -> **SELL** — confirmed by the trade label (`Sale`/`Sold`/`Redemption`/`Maturity`
  -> SELL; `New`/`Purchase`/`Bought` -> BUY). A genuine zero-cash transfer is an `ASSET RECEIPT` /
  `ASSET DELIVERY` (side from the description: `Receive Free of Payment` -> RECEIPT, `Delivery Free of
  Payment` -> DELIVERY, else the quantity sign).
- **Free-of-payment transfers carry no price/value** — priced from our own data: the most-recent
  `Portfolio.CustodyPosition` implied unit value (`Value / Quantity`, which already embeds
  ContractSize); else PENDING at Value 0 (sentinel Price so the proc stores an exact 0).
- **Price** is backed out so `Price * Quantity * ContractSize == Value`:

  | family | ContractSize | Price |
  |---|---|---|
  | equity option | 100 | `|Value| / (|Qty| * 100)` |
  | bond / fixed income | 0.01 (per-100) | `|Value| / (|Qty| * 0.01)` |
  | time deposit | 0.01 (per-100) | `|Value| / (|Qty| * 0.01)` (value includes accrued interest on withdrawal) |
  | equity | 1 | `|Value| / |Qty|` |
  | future | 1 | sentinel (Value 0) |
  | NDF | 1 | sentinel (Value 0) |

- **Cash / GL** rows are `Asset = USD`, `Quantity = Value`, `Price = 1.0`.

### Asset resolution & auto-registration
- Canonical resolved via `AssetCustody` (by `TickerCustody` = BTG `assetId`) / `Global.v_Asset` (by
  ISIN) / exact Description.
- **Time Deposits and NDFs auto-register** in `Global.Asset` (`TD<assetId>` / `NDF<assetId>`) plus
  their `Portfolio.AssetCustody` maps — emitted as `Global.Asset_Update I` + `AssetCustody_Update I`
  items at the head of the first account's batch (skipped when the asset already exists, q8). Options
  matched by exact Description get an `AssetCustody_Update I` map queued too.

---

## 5. System coherence (enforced by the parser before the batch is built)

- **Valid types only.** `TransactionType` must exist in `Global.TransactionType` and
  `GeneralLedgerType` (when set) in `Global.GeneralLedgerType`. Unknown -> **dropped and reported**
  (`dropped_invalid`), never inserted.
- **Status** is restricted to `VALIDATED, IGNORED, PENDING, UPDATED`:
  - **VALIDATED** — asset resolves to a canonical **and** quantity + price are present. Feeds position
    calc directly.
  - **PENDING** — asset can't be resolved, a Free-of-Payment transfer with no price, an option expiry
    whose held side is unknown, or an income GL whose payer can't be identified. **Inserted** (never
    dropped — keeps the movement tracked), excluded from position calc; resolve the asset/price and
    re-run to upgrade to VALIDATED.

---

## 6. CheckedDate gate (the most important guard)

A transaction is **never** written on or before an account's active `BTG Cayman` CheckedDate
(`Portfolio.v_CheckedDate`, `Custody='BTG Cayman'`, `Activated=1`). A row is **blocked when
`Date <= CheckedDate` OR `SettlementDate <= CheckedDate`** (mirroring the proc). The parser **excludes**
blocked rows from the batch — critical under `execute_batch`, because one blocked row would RAISERROR
and roll back the **whole account**. Blocked rows are reported (`lock_blocked`), not inserted.

---

## 7. Duplicate detection

The feed returns the same trade under several operation-days (weekend repeats; T+1 trades on two
days). Rows are de-duplicated **per account by `(tradeId, |value|)`** (the `tradeId` from
`RawTransaction`), applied **both within the batch and against rows already booked** (q7). A row with
no `tradeId` falls back to `(Date, TransactionType, CustodyIdentifier, Value)`.

**Why `|value|`, not `tradeId` alone.** One broker `tradeId` can carry several **distinct legs that
differ in amount** — a $50 wire fee *and* the principal withdrawal; a TD `New` *and* its `Maturity`.
Keying on `tradeId` alone collapses them and drops every leg but the first; `|value|` keeps each.
Conversely, BTG reports the **same** event under one tradeId and the **same amount** in more than one
day/section (a cash dividend in `equities` *and* `cash`) — `(tradeId, |value|)` books that **once**.

Because `execute_batch`/`AccountTransaction_Update` has no bulk delete-by-window, this existence
check is what makes re-runs safe (`skipped_dup`). To **correct** a leg already booked wrong, delete it
(`@CMD='D'`) first, then re-run the window.
