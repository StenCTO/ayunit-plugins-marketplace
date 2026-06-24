---
name: btg-offshore
description: Load BTG Offshore (Cayman) account transactions into Portfolio.AccountTransaction for one or more accounts over a date range, live via the ayunit MCP. Pulls the feed with get_btg_offshore_trades, maps every event (security trades -> BUY/SELL; cash/interest/fees -> DEPOSIT/WITHDRAW/GENERAL LEDGER; income -> GL INTEREST/DIVIDEND; NDF lifecycle; futures; option expiry), auto-registers TDs/NDFs, and commits each account atomically through execute_batch (Portfolio.AccountTransaction_Update @CMD='I'). Custody/Source/Broker is "BTG Cayman". Default scope is ALL accounts for the latest 5 days until today. Use whenever the user asks to load/ingest/book BTG offshore or BTG Cayman transactions, trades, cashflow or movements (PT or EN: "carrega as transacoes do BTG Cayman da conta X", "load BTG offshore transactions for the last week").
---

# Load BTG Offshore (Cayman) transactions into `Portfolio.AccountTransaction`

You orchestrate turning the **`get_btg_offshore_trades`** feed into validated
`Portfolio.AccountTransaction` rows for one or more accounts over a date range, then commit them.
Custody / Source / Broker is `BTG Cayman`; accounts are USD / offshore; org `Sten` / `AgnesOrg00DB`.
This is the **transaction sibling of the `custody-position-btg-offshore` loader** — same architecture
and guardrails, source is the **ayunit MCP, not a file**, and the BTG-specific field treatment is
the executable encoding in `scripts/parse_btg.py` + [`mapping.md`](mapping.md).

## Single source of truth — read first
- **The ayunit live DB is the one source of truth.** No hard-coded account maps or asset ids — read
  them live via the **ayunit MCP**.
- **All DB access via the ayunit MCP** — `execute_select_query` for every read; **`execute_batch`**
  to commit each account in one atomic transaction. Allowlisted writes used here:
  `Portfolio.AccountTransaction_Update` (`I`), `Global.Asset_Update` (`I`, TD/NDF),
  `Portfolio.AssetCustody_Update` (`I`, maps). The feed read is `get_btg_offshore_trades`.
- **`scripts/parse_btg.py` does only LOCAL work** — it maps the saved feed + the lookups you fetched
  into a review-able plan and the ready-to-commit `execute_batch` items. It never touches the DB.
- **The credential profile (`access_name`) is `BTG Sten Offshore`** — always; don't ask.

## Inputs (all optional)
- **Accounts** — one or more BTG `accountNumber`s. Default: **all** accounts under the credential.
- **Date range** — `start_date` / `end_date` (`YYYY-MM-DD`). Default: **the latest 5 days until
  today** (the feed caps a window at 7 days; split a longer range into <=7-day calls).

Echo the resolved scope back to the user. Reply in the user's language (PT/EN).

## Run autonomously — commit directly (the default)
The guards make the write safe without a human gate: it is **idempotent** (per-account dedup by
`(tradeId, |value|)`, both within the batch and against rows already booked), it can only touch
dates **after** the account's active `BTG Cayman` CheckedDate (lock-gated locally **and** by the
proc), and unmappable rows land as **PENDING** (never as bad VALIDATED data). So fetch -> map ->
`execute_batch` straight through, per account. **Do not stop to ask "should I commit?".** Stop only
to surface a genuine problem (a feed/auth failure, a `dropped_invalid > 0`, or a batch that fails its
`dry_run` validation / commit assert) or to deliver the final report.

## The load cycle

### 1 — Pull the feed (read-only) and save it
For each <=7-day sub-window, call
`mcp__ayunit__get_btg_offshore_trades(access_name='BTG Sten Offshore', accounts=[<accts or omit>], start_date=<d>, end_date=<d>)`.
Save each raw payload (the `{"result":"..."}` wrapper is fine) to `trades_<start>_<end>.json`. If the
result comes back as a file path (large), use that path; if inline, write it to disk yourself — the
parser reads from disk. A transient "connection attempts failed" is worth a retry or two.

### 2 — Emit the lookup SELECTs (run the parser over ALL raw files at once)
```
python "${CLAUDE_PLUGIN_ROOT}/skills/btg-offshore/scripts/parse_btg.py" trades_*.json
```
Prints up to **10 concrete SELECTs** (q1 AssetCustody, q2 ISIN->Asset, q3 Description->Asset,
q4 valid Transaction/GL types, q5 CheckedDate locks, q6 held assets for income matching,
q7 existing transactions for dedup + expiry net, q8 TD/NDF already registered, q9 inception seed,
q10 free-of-payment unit prices) — already scoped to `BTG Cayman`, the accounts and the assetIds in
the feed, with **no placeholders to fill**. (q10 only matters if the feed has Free-of-Payment
transfers; run it then, otherwise it's harmless/empty.)

### 3 — Run the SELECTs and let the parser assemble the lookups
Run each printed SELECT **verbatim** with `execute_select_query` (DB `AgnesOrg00DB`) and save each
**raw result** to the file the parser names (`q1.json` ... `q10.json`). Save whatever the connector
returns — `load_rows` tolerates the `{result|columns|rows}` envelope, so never reshape. Then:
```
python ".../scripts/parse_btg.py" trades_*.json --build-lookups \
   --q1 q1.json --q2 q2.json --q3 q3.json --q4 q4.json --q5 q5.json \
   --q6 q6.json --q7 q7.json --q8 q8.json --q9 q9.json [--q10 q10.json] --out shared.lookups.json
```

### 4 — Build the plan + batch items
```
python ".../scripts/parse_btg.py" trades_*.json --lookups shared.lookups.json --batch ./batch_out
```
Writes one `*.plan.json`, one `items_<account>.json` per account (dependency order: TD/NDF
**registrations** -> **AssetCustody maps** -> **AccountTransaction inserts**; shared registrations/maps
land in the first account's file), and `manifest.json`. The review print shows, per account: counts by
bucket and TransactionType, and **validated / pending / lock_blocked / dropped_invalid / skipped_dup**.

### 5 — Commit each account (autonomous; only surface on a tripped gate)
For each account in `manifest.json`:
1. `mcp__ayunit__execute_batch(database='AgnesOrg00DB', items=<items_<account>.json>, dry_run=true)`
   — validates and counts every EXEC, no DB writes. **Gate:** every item must come back validated
   (count == `len(items)`). Green -> commit. Fails -> **stop and surface it**.
2. `mcp__ayunit__execute_batch(database='AgnesOrg00DB', items=<items_<account>.json>, dry_run=false)`
   — commits the whole account atomically (all-or-nothing). On any item failure the **entire account
   rolls back** (`failed_index`/`error` say which) — report and stop.
3. **Assert `statements_run == len(items)`** on the commit. If short, an item was dropped — stop and inspect.

> **Why per account, not one giant batch.** `execute_batch` takes `items` **inline**. One batch per
> account keeps each payload small enough to pass faithfully and each account atomic; the
> `statements_run` assert catches any drop immediately. Registrations/maps ride in the first account's
> batch (the parser already orders them).

### 6 — Verify & report
```sql
SELECT ClientAccount, TransactionType, Status, COUNT(*) n, SUM(Value) net_value
FROM Portfolio.v_AccountTransaction
WHERE Custody='BTG Cayman' AND ClientAccount='<account>'
  AND CAST(Date AS date) BETWEEN '<start>' AND '<end>'
GROUP BY ClientAccount, TransactionType, Status ORDER BY TransactionType;
```
Confirm the loaded count matches the plan's writable count. Report per account: **inserted VALIDATED /
inserted PENDING / skipped(dup) / lock_blocked / registered (TD/NDF) / option-maps / dropped(invalid)**.
Remind the user that PENDING rows await asset registration (then a re-run upgrades them to VALIDATED).
If `lock_blocked > 0`, name the account(s) — those dates are frozen by CheckedDate.

## What to expect (normal, not errors)
- **PENDING rows** — a security whose canonical asset isn't mapped yet (new option/future/bond), a
  Free-of-Payment transfer with no price (booked at Value 0), an option expiry whose held side is
  unknown, or a Cash-ledger dividend/interest/distribution whose paying security can't be resolved
  (booked as GL with no AssetRelated). Inserted, excluded from position calc, fixed in place by
  registering the asset/price and re-running. **Never dropped.** NDF maturities are **not** PENDING.
- **skipped(dup)** — the same trade returned under several days (BTG repeats the last business day on
  weekends; T+1 trades appear on two days) or already booked. Deduped per account by `(tradeId,|value|)`.
  Safe to re-run.
- **lock_blocked** — rows on/before an account's active `BTG Cayman` CheckedDate. Excluded from the
  batch (the proc would RAISERROR and roll the whole account back) and reported. Expected when a window
  overlaps a lock.
- **dropped(invalid)** — a row whose TransactionType/GL type isn't registered. Should be 0; if not, the
  feed has a shape the mapper doesn't classify — inspect `parse_btg.py`.
- **Only Time Deposits and NDFs auto-register.** Other new assets stay PENDING until mapped by hand.

## Critical rules
- **CheckedDate is sacred.** Never write on/before an account's active `BTG Cayman` CheckedDate — the
  parser excludes those rows (`lock_blocked`) so one of them can't roll back the whole atomic batch;
  the proc enforces it too. Surface, don't force. Moving a lock is a separate, user-approved step.
- **No duplicates.** Dedup per account by `(tradeId, |value|)` (intra-batch + vs the DB): distinct legs
  sharing one tradeId (a fee + its withdrawal; a TD New + its Maturity) are each kept; the same event
  echoed across days/sections (a dividend in Equities *and* Cash) is booked once. Re-runs are safe.
- **Option expiry side comes from the held position, never the raw quantity.** An OTM expiry
  (`tradeDescription`~`Expiry`, `value=0`) carries an unsigned quantity. The parser closes the side we
  actually hold — **BUY** to cover a net short, **SELL** to close a net long — from the inception seed
  (AccountPosition at the CheckedDate) plus the net of all post-inception legs (DB union this batch). A
  zero/unknown held side stays PENDING. Never infer the side from the feed's quantity sign.
- **Direction from cash sign / trade label** (security trades). `value < 0` -> BUY, `value > 0` -> SELL;
  the trade label (Sale/Purchase/New/Maturity/Redemption) confirms it. Generic cash/fees become
  DEPOSIT/WITHDRAW/GENERAL LEDGER. (NDF and futures are exceptions — their side is NOT the cash sign.)
- **Income is GL, not a trade.** Bond coupons, equity dividends and fund income/distributions are
  `GENERAL LEDGER RECEIPT`/`DELIVERY` (`INTEREST/DIVIDEND`), Asset `USD`, AssetRelated = the paying
  security, Quantity = Value, Price 1 — **never** a BUY/SELL (booking a coupon as SELL ships the whole
  bond face out). Includes Cash-ledger fund distributions ("Cash Dividend ..." / "... Cash
  Distribution") — never a DEPOSIT. The payer is resolved by description (exact, else distinctive-token
  overlap among the account's HELD assets, e.g. `Pearl Diver ...` -> `PEARL DIVER`); unresolved -> PENDING.
  `Daily Interest` stays account-level overnight cash GL (`OVERNIGHT`, no AssetRelated).
- **NDF / FX-forward lifecycle.** Position legs are **always Qty 1 @ Value 0 / Price 0**, side fixed by
  lifecycle (NOT the USD-leg sign): `New` -> **BUY 1**, `Maturity` -> **SELL 1** (so the
  transaction-built position matches the custody snapshot, which carries an open NDF at Qty +1). The
  **net cash settlement comes from the Cash ledger** (`productType=FXNDF`, `transferType=PRINCIPAL`):
  a `Forward Maturity` GL RECEIPT/DELIVERY, Asset `USD`, AssetRelated `NDF<tradeId>`, Value = the net
  cash, **dated on the settlement date**. The two Value-0 legs get phase-suffixed tradeIds
  (`:NDF_NEW`/`:NDF_MAT`) so they don't collide under `(tradeId,|value|)`.
- **Futures = no cash on the trade leg.** A future BUYs/SELLs the **quantity traded** @ Value 0 / Price 0
  (margin, not notional — direction from the quantity sign / Sale-Purchase label, never the feed
  `value`; booking the notional as cash drains the USD balance — the historical bug). A future's cash
  comes from the Cash ledger: its **commission** (`CLIENT_FEE_FUT`, "... Commission") -> a FEE GL, and
  its **realized P&L** (`transferType=REALIZED_PL`) -> a `GENERAL LEDGER RECEIPT`/`DELIVERY`
  `INTEREST/DIVIDEND` with `AssetRelated` = the futures asset (linked by `tradeId`). The realized P&L
  is VALIDATED once the future is registered, else PENDING (linked on re-run) — never dropped.
- **Free-of-payment transfers are priced by us.** `Delivery/Receive Free of Payment` carry no
  price/value -> `ASSET DELIVERY`/`RECEIPT` priced from the latest CustodyPosition unit value (q10);
  no price -> PENDING at Value 0. ContractSize is always applied.
- **No double-counting.** Security trades come from the detail sections; the Cash ledger contributes
  only genuine cash/GL events (`transferType=SECURITY` legs, `StructuredFlows` TD flows, fund legs and
  internal margin transfers are excluded). Weekend/holiday repeats are dropped (`_operation_date` !=
  `positionDate`).
- **Pass absolute `Quantity`/`Price`/`Value`** — the proc signs them from `TransactionType`. **Never
  pass `AccountCurrency`/`AccountFx`** (the proc computes them; the wrapper rejects them).
- **Never invent asset ids.** `@Asset` is the resolved canonical (or `USD`); the proc + AssetCustody
  resolve it. TDs/NDFs are the only auto-registrations.
- **Source of truth = `scripts/parse_btg.py` + `mapping.md`.** Format treatment + coherence + gate +
  dedup all live in the parser, documented in `mapping.md`. If they disagree, fix both together.

## When unsure
- **A new asset that won't resolve** -> the row lands PENDING (tracked, not lost). Register it
  (asset-register) + add its `Portfolio.AssetCustody` map, then re-run the window to upgrade it to
  VALIDATED.
- **A TransactionType/GL the mapper doesn't classify** (`dropped_invalid > 0`) -> inspect the feed row
  and add the rule to `parse_btg.py` (`classify_cash` / the per-section handlers).
- **A window overlaps a CheckedDate** -> those rows are `lock_blocked` and skipped; that's correct.
  Don't move the lock just to force them in.
