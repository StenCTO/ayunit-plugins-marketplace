---
name: position-quantity-adjustment
description: "Use when the user wants to absorb the tiny CustodyPosition ↔ AccountPosition differences that survive the daily reconciliation — fractional dust on quota counts (e.g. custody 524,546.0010 vs ours 524,545.9363, Δ +0.0647 on a Sten Master D30 holding) AND rounding gaps on cash balances (e.g. custody BRL 236.62 vs ours 236.57, Δ +0.05). The skill scopes by account list and a date (or window), pulls v_CustodyPosition vs v_AccountPosition per (Account, Asset, Date), keeps only the rows whose |Δqty| sits under the configured tolerance, and inserts the right shape per asset kind: for NON-CASH assets (Asset ≠ Currency) a BUY (custody > ours) or SELL (custody < ours) for ABS(Δ) with ValueGross=ABS(Δ)*<price from AssetData.v_Price on-or-before the trade date>, Value=0 (no cash leg), PriceExFee=<price>, Price=0; for CASH assets (Asset = Currency, i.e. BRL/USD/EUR/…) a GENERAL LEDGER RECEIPT (custody > ours) or GENERAL LEDGER DELIVERY (custody < ours) for ABS(Δ) with full cash impact (Value=Quantity=ValueGross=ABS(Δ), Price=1) so the cash position itself reconciles. Both shapes land Status=UPDATED with Obs='automatic quantity adjustment' (or 'automatic cash adjustment' for GL). Lock-aware (must be run BEFORE the CheckedDate is advanced over the target date — adjustments on/at the lock are rejected by the procedure), and explicitly out-of-scope for sign-flip-sized differences and for anything above tolerance — those signal a real missing trade and must be reviewed, not papered over. Trigger when the user says: 'absorb the dust', 'criar ajuste de quantidade', 'ajusta a diferença de caixa', 'há uma pequena diferença em XYZ entre custódia e nossa posição, cria um ajuste', 'reconcile small position breaks', or asks for a daily/period sweep of tiny breaks across one or many accounts."
---

# Absorb tiny CustodyPosition ↔ AccountPosition quantity breaks

You are the orchestrator for **booking the dust** between what custody says the account
holds (`Portfolio.v_CustodyPosition`) and what our pipeline calculates from
`Portfolio.AccountTransaction` (`Portfolio.v_AccountPosition`). The vast majority of these
breaks are real — a missing trade, a wrong sign, an unmapped `AssetR` — and must be
investigated. **This skill targets only the residue**: fractional drift the size of a
quota-rounding error (e.g. `+0.0647` on a 524,546-unit holding) that no real trade
explains and that nobody wants to chase one ticket at a time.

For each tiny break, the skill inserts **one new** `Portfolio.AccountTransaction` row that
closes the gap to custody. The shape depends on whether the broken row is a security
position or a cash balance:

- **Non-cash assets** (`Asset ≠ Currency`) → **`BUY`** / **`SELL`** for `ABS(Δ)` with
  `ValueGross = ABS(Δ) * Price` (so the cost-basis / performance side carries the trade)
  but **`Value = 0`** (no cash leg) — the asset quantity moves and `AvgPrice` ingests the
  adjusted lot, but cash does not, because there is no real cash settlement to match.
- **Cash assets** (`Asset = Currency`: `BRL`, `USD`, `EUR`, …) → `GENERAL LEDGER RECEIPT` /
  `GENERAL LEDGER DELIVERY` for `ABS(Δ)` with **full cash impact** (`Value = Quantity =
  ValueGross = ABS(Δ)`, `Price = 1`) — the broken row *is* the cash position, so the
  correction must move cash.

After the adjustment, the next PortfolioCreator pass reconciles cleanly and the human can
advance the `CheckedDate`.

This is a **self-contained orchestration skill**. It is **non-destructive** by design (only
`@CMD='I'`), it **never modifies the lock**, and it never touches breaks above tolerance —
those are real defects and are *reported* for the operator, never absorbed.

## Inputs

- **Account list** (one or many `ClientAccount` strings) — required.
- **A date OR a `[Date_From, Date_To]` window** — required. Default window is the single
  reconciliation date; a range sweeps each business date inside it.
- *Optional* — tolerance overrides (see §3). The defaults are deliberately conservative.

Echo the resolved scope (accounts / window / tolerance) at the start of every report.

> **Account key format.** XP onshore accounts are **not** zero-padded (`4789186` stays
> `'4789186'`). BTG onshore accounts are zero-padded to 9 (`47067` → `'000047067'`). When
> the user gives a short number, resolve it first with
> `SELECT DISTINCT ClientAccount, Custody FROM Portfolio.v_AccountTransaction
> WHERE ClientAccount LIKE '%<n>%'` and reuse the exact stored string.

## Reference resources (read on demand)

| Resource | Read when… |
|---|---|
| [`ayunit://docs/position/reconciliation`](ayunit://docs/position/reconciliation) | **First.** The 3-way (`AccountPosition.QuantityClose` vs `CustodyPosition.Quantity`) comparison and the "why divergences happen" table — every divergence on this list must be eliminated *before* you classify a break as "dust" worth absorbing. |
| [`ayunit://docs/transaction/types`](ayunit://docs/transaction/types) | The `BUY` / `SELL` sign rules (Qty +/−, Price +, ValueGross ∓, Value ∓) and the `AssetRelated = Asset` rule on a `VALIDATED`/`UPDATED` asset row; the `GENERAL LEDGER RECEIPT` / `DELIVERY` sign rules (Asset = Currency, Price forced to 1, Quantity = Value = ValueGross synchronized, AssetRelated optional — `NULL` is fine for a reconciliation plug). |
| [`ayunit://docs/transaction/procedure`](ayunit://docs/transaction/procedure) | `Portfolio.AccountTransaction_Update` params. Asset-side auto-validators (1.2 — auto-fill price when **all three** of Price/Value/ValueGross are zero — does **not** fire here because we pass `ValueGross > 0`; 1.4 — recompute `Price = ABS(Value/(Quantity*ContractSize))` always fires, which is fine: with `Value=0` it returns 0, exactly the convention). On the GL side the proc forces `Price = PriceExFee = 1` and `Quantity = Value = ABS(...)`, so passing absolute `Value` is enough. |
| [`ayunit://docs/checkeddate/usage`](ayunit://docs/checkeddate/usage) | **Before any write.** A `VALIDATED`/`UPDATED` insert is rejected when `Date <= CheckedDate OR SettlementDate <= CheckedDate`. So this skill must run **before** the daily lock is advanced over the target date. |
| [`ayunit://docs/transaction/fixes`](ayunit://docs/transaction/fixes) | Universal write guardrails (drop `AccountCurrency`/`AccountFx`, absolute values, always set `AgentCheck`). |

## Tools you call directly

- `execute_select_query` — every read (scope, custody-vs-portfolio diff, prices, locks, verify).
- `get_view_detail` / `get_procedure_detail` — confirm columns/params; never guess.
- `execute_procedure(procedure='Portfolio.AccountTransaction_Update', cmd='I', …)` —
  one-shot inserts (small lists / canary).
- `execute_batch(items=[… cmd='I' …], dry_run=…)` — multi-row commits. **Always** dry-run
  first, show the plan, then `dry_run=false`. `@CMD='I'` is not destructive, so
  `allow_destructive` is not required.

## The adjustment cycle

### 1 — Pull the per-asset diff (this is the scope)

One query per account in scope, joined on `(Account, Asset, Date)`. Read from the views.

```sql
SELECT cp.Account, cp.Date, cp.Asset, cp.Description, cp.Currency,
       CASE WHEN cp.Asset = cp.Currency THEN 'CASH' ELSE 'ASSET' END AS Kind,
       cp.Quantity AS CustodyQty,
       ISNULL(ap.QuantityClose, 0) AS PortfolioQty,
       (cp.Quantity - ISNULL(ap.QuantityClose, 0)) AS Diff,
       cp.Price AS CustodyPrice, cp.PriceDate
FROM Portfolio.v_CustodyPosition cp
LEFT JOIN Portfolio.v_AccountPosition ap
       ON ap.Account = cp.Account AND ap.Asset = cp.Asset AND ap.Date = cp.Date
WHERE cp.Account IN (…)
  AND cp.Date BETWEEN <from> AND <to>
  AND cp.Asset IS NOT NULL           -- skip untranslated custody rows (fix AssetCustody first)
ORDER BY cp.Account, cp.Date, ABS(cp.Quantity - ISNULL(ap.QuantityClose, 0)) DESC;
```

> The `Kind` column drives the §6 dispatch: `'CASH'` (`Asset = Currency`) → GL receipt /
> delivery with cash impact; `'ASSET'` → asset receipt / delivery with no cash leg.

> Always join on the **canonical** `Asset` (not `AssetR`) — `v_CustodyPosition` left-joins
> Global lookups, so an untranslated row has `Asset = NULL` and would never pair with
> `v_AccountPosition` anyway. Such rows belong to the `AssetCustody`/translation flow,
> not here — report them and move on.

### 2 — Classify each break

For every row of the diff, decide which bucket it lands in. Only **dust** is absorbed.

| Bucket | Condition | Action |
|---|---|---|
| **Match** | `Diff ≈ 0` | nothing to do |
| **Dust** | `0 < ABS(Diff) ≤ tolerance` (see §3) | candidate adjustment (this skill) |
| **Real break** | `ABS(Diff) > tolerance` **or** sign-flip-sized (e.g. \|Diff\| ≈ 2·CustodyQty) | report only — likely a missing trade, wrong sign, lending/margin, or stale price; route to `duplicate-trade-reconcile`, the loader for that custody, or human review |
| **Custody-missing** | row in `v_AccountPosition` but not in `v_CustodyPosition` (or vice-versa with `Asset` resolved but no portfolio side) | report — not dust; investigate per `position/reconciliation` |

> **Hard rule.** A break can be both *small in magnitude* and *not dust* — e.g. an FII paid a
> single-share dividend that landed as a `BUY` with `Quantity = 1.0` on the wrong day, so
> the position is off by exactly 1.0 unit. If a real round-number trade explains the gap,
> the skill must **not** absorb it. The tolerance below is set for **fractional** dust
> (sub-1 quota or sub-1 currency unit) precisely for this reason.

### 3 — Tolerance (defaults + when to override)

The thresholds differ by `Kind` because the magnitudes mean different things — fractional
*units of an asset* vs *units of currency*.

**a. `ASSET` rows** (`Asset ≠ Currency`) — dust when **both** are true:
- **Absolute quantity floor** — `ABS(Diff) < 1.0` *unit of the asset*. Fractional only;
  integer quantity differences (`1`, `2`, `5`, …) are presumed-real trades. Override
  only when the asset's natural unit is *itself* fractional (e.g. some forwards).
- **Cash-impact ceiling** — `ABS(Diff) * Price < 5` *units of the asset's currency*
  (≈ 5 BRL, 5 USD, …). Even tiny `Diff` on a high-NAV fund can be material; this
  keeps the auto-absorbed cost-basis nudge negligible.

**b. `CASH` rows** (`Asset = Currency`) — dust when:
- `ABS(Diff) < 5` *units of the currency itself* (≈ 5 BRL, 5 USD, …). For cash the
  quantity *is* the cash value (`Price` is forced to 1), so there is no separate
  cash-impact gate — the single threshold already bounds both. Sub-cent rounding gaps
  always qualify; anything that looks like a missing deposit/withdraw (round 10/100/1000
  amounts, or > the threshold) is reported as a real break, never absorbed.

The user can override either threshold per run, but **never silently** — echo the
override in the scope line.

### 4 — Price lookup (ASSET rows only)

**Skip this step for `CASH` rows** — the GL shape forces `Price = PriceExFee = 1`, no
external price is needed.

For each `ASSET` dust row, pull the asset's price on (or just before) the trade date:

```sql
SELECT TOP 1 Price
FROM AssetData.v_Price
WHERE Asset = '<asset>' AND [Date] <= '<trade-date>'
ORDER BY [Date] DESC;
```

This price becomes the **`PriceExFee`** on the adjustment trade and drives `ValueGross`.
If multiple `Source`s exist for the same date, pick the canonical one for the custody
(`BTG` for BTG-fed funds, `Anbima` for ANBIMA funds, etc.) — when in doubt, take the
first one in the ordered result. **If no price is found** (truly unpriced asset), do
not write the adjustment: report the row and stop for that asset.

### 5 — Lock-gate (CheckedDate) — non-negotiable

Read the active lock per `(Account, Custody)` in scope:

```sql
SELECT Account, Custody, [Date], Activated
FROM Portfolio.v_CheckedDate
WHERE Account IN (…) AND Activated = 1 ORDER BY Account, [Date] DESC;
```

A `VALIDATED`/`UPDATED` row is **rejected** by the procedure when
`Date <= CheckedDate OR SettlementDate <= CheckedDate`. **Both** the trade date and the
settlement date are the same here (the reconciliation date), so the rule simplifies to:
**adjustment date must be strictly > active lock date for that `(Account, Custody)`**.

- If `lock_date >= adjustment_date` → **lock-blocked**: report, do not write, and **do
  not move the lock**. The natural workflow is: the operator runs this skill as part of
  the daily reconciliation *for date `D`*, with the active lock still at `D-1`. After the
  skill commits the dust adjustments, the operator advances the lock to `D` via the
  CheckedDate write path. Moving an already-advanced lock backward is a recoil-cycle
  decision, not this skill's job (emit a copy-paste pointer to
  [`ayunit://docs/checkeddate/usage`](ayunit://docs/checkeddate/usage) and stop).
- Flag any `(Account, Custody)` with **>1** `Activated=1` row — the procedure's scalar
  subquery breaks (error 512) on duplicates, so *every* write on that account fails. Skip
  those accounts; report them.

### 6 — Build & commit the adjustments

Two shapes, dispatched by the row's `Kind` from §1:

**6.a — `ASSET` rows** (`Asset ≠ Currency`, no cash leg)

| Sign of `Diff` (custody − ours) | `TransactionType` | `Quantity` (abs) | `Value` | `ValueGross` (abs, pre-sign) | `Price` | `PriceExFee` |
|---|---|---:|---:|---:|---:|---:|
| `Diff > 0` (custody > ours — give us more) | **`BUY`** | `ABS(Diff)` | `0` | `ABS(Diff) * Price` | `0` | `<price>` |
| `Diff < 0` (custody < ours — take some away) | **`SELL`** | `ABS(Diff)` | `0` | `ABS(Diff) * Price` | `0` | `<price>` |

Pass **absolute** values for all numeric fields; the procedure applies the sign
(`BUY` ⇒ Qty +, Value −, ValueGross −; `SELL` ⇒ Qty −, Value +, ValueGross +). With
`Value = 0`, the signed `Value` stays `0` either way → no cash leg, while
`ValueGross = ABS(Diff)*Price` carries the lot into `AvgPrice` (BUY) / performance
attribution (SELL).

> **Why `BUY`/`SELL`, not `ASSET RECEIPT`/`ASSET DELIVERY`.** Both pairs share the same
> sign table, but `ASSET RECEIPT`/`DELIVERY` are reserved for **in-kind transfers** of
> shares between custodies (no economic acquisition/disposal — just a custody move). A
> quantity-reconciliation plug is *not* a transfer; it represents a fractional purchase
> or sale that custody booked and our pipeline didn't see, so it must land as `BUY`/`SELL`
> for the position attribution to read correctly.

Per-row params (full shape — `@CMD='I'`):

```
{
  "Date": "<reconciliation-date>",
  "SettlementDate": "<reconciliation-date>",
  "ClientAccount": "<acct>", "Broker": "<custody>", "Custody": "<custody>",
  "TransactionType": "BUY" | "SELL",
  "Currency": "<asset.Currency>",
  "Asset": "<asset>", "AssetRelated": "<asset>",
  "Quantity": ABS(Diff),
  "Price": 0,
  "PriceExFee": <AssetData.v_Price.Price>,
  "ValueGross": ABS(Diff) * <PriceExFee>,
  "Value": 0,
  "Status": "UPDATED",
  "Obs": "automatic quantity adjustment",
  "AgentCheck": "fix YYYY-MM-DD: position-quantity-adjustment — Asset <asset>, Diff <±x.xxxx> (custody <CustodyQty> vs ours <PortfolioQty>); absorbed via <BUY|SELL> Qty=ABS(Diff), Value=0, ValueGross=ABS(Diff)*<price>, PriceExFee=<price> from AssetData.v_Price@<priceDate> [PQA]"
}
```

**6.b — `CASH` rows** (`Asset = Currency`, full cash impact)

| Sign of `Diff` (custody − ours) | `TransactionType` | `Quantity` / `Value` / `ValueGross` (abs) | `Price` / `PriceExFee` |
|---|---|---:|---:|
| `Diff > 0` (custody has more cash than ours — book a receipt) | **`GENERAL LEDGER RECEIPT`** | `ABS(Diff)` | `1` |
| `Diff < 0` (custody has less cash than ours — book a delivery) | **`GENERAL LEDGER DELIVERY`** | `ABS(Diff)` | `1` |

For GL the procedure synchronises `Quantity = Value = ValueGross` from `ABS(Value)`, sets
`Price = PriceExFee = 1`, and applies the sign by type (RECEIPT all positive; DELIVERY all
negative). So passing `Value = ABS(Diff)` is sufficient — but pass `Quantity`, `Value`,
`ValueGross`, `Price`, and `PriceExFee` explicitly for readability in the batch preview.

> **`Asset` and `AssetRelated` on the GL row.** `Asset = Currency` is mandatory and is
> what makes this a *cash* GL (e.g. `Asset = 'BRL'`). **`AssetRelated` must be `NULL`** on
> a reconciliation plug — there is no originating security; if you set `AssetRelated`
> equal to the cash asset you'll tie the cash flow back to itself and double-attribute it
> in income-attribution reports.

Per-row params (full shape — `@CMD='I'`):

```
{
  "Date": "<reconciliation-date>",
  "SettlementDate": "<reconciliation-date>",
  "ClientAccount": "<acct>", "Broker": "<custody>", "Custody": "<custody>",
  "TransactionType": "GENERAL LEDGER RECEIPT" | "GENERAL LEDGER DELIVERY",
  "Currency": "<currency>",
  "Asset": "<currency>",          // = Currency for cash GL
  "AssetRelated": null,            // reconciliation plug — no originating security
  "Quantity": ABS(Diff),
  "Price": 1, "PriceExFee": 1,
  "ValueGross": ABS(Diff), "Value": ABS(Diff),
  "Status": "UPDATED",
  "Obs": "automatic cash adjustment",
  "AgentCheck": "fix YYYY-MM-DD: position-quantity-adjustment — Cash <currency>, Diff <±x.xx> (custody <CustodyQty> vs ours <PortfolioQty>); absorbed via <GENERAL LEDGER RECEIPT|GENERAL LEDGER DELIVERY> Value=ABS(Diff) [PQA]"
}
```

> **Never pass `AccountCurrency`/`AccountFx`** — they're computed and the MCP wrapper rejects
> the whole payload (generic 400) if present.

**Preview → confirm → commit.** For 1–2 adjustments, run them one-shot via
`execute_procedure` (a canary is enough). For more, build `execute_batch(items=[…],
dry_run=true)` first, present the per-row before/after table to the user, get the green
light, then `dry_run=false`. The batch is atomic — any mid-batch failure rolls everything
back.

### 7 — Verify

Re-run the §1 diff query for the same scope. Expected: every absorbed row's
`Diff ≈ 0` now (within float epsilon). Any non-zero remaining is a real break and must
appear in the §6 report bucket — never silently re-absorbed on a second pass.

Also re-SELECT each inserted `pk_AccountTransactionID`: confirm `Status = 'UPDATED'`,
`TransactionType` is the chosen side, `Quantity = ±ABS(Diff)`, `AgentCheck` carries the
`[PQA]` tag, and the value columns match the kind — `Value = 0` for `ASSET` rows;
`Value = ±ABS(Diff)` (signed by RECEIPT/DELIVERY) for `CASH` GL rows.

### 8 — Report

End every run with these buckets:

| Bucket | Meaning | Disposition |
|---|---|---|
| **Absorbed** | dust rows for which an adjustment was inserted + verified | done |
| **Reported — real break** | `ABS(Diff) > tolerance` or sign-flip-sized | route to `duplicate-trade-reconcile`, the loader for that custody, or human review |
| **Reported — custody-missing / portfolio-missing** | row exists on only one side (with `Asset` resolved) | investigate per `position/reconciliation` (translation, missing trade, lending) — **not** dust |
| **Reported — no price** | `ASSET` dust row but `AssetData.v_Price` has nothing on/before the date (`CASH` rows are not affected) | price the asset first, then re-run |
| **Lock-blocked** | adjustment date ≤ active CheckedDate for that `(Account, Custody)` | run this skill *before* advancing the lock; do **not** move the lock from here |
| **Skipped — broken lock** | `(Account, Custody)` has >1 active CheckedDate | fix the duplicate lock first; report |
| **Skipped — untranslated** | `cp.Asset IS NULL` (custody row without an `AssetCustody` map) | fix the translation first (`Portfolio.AssetCustody`); not dust |

## Critical rules

- **Only dust under tolerance.** `ASSET` rows: `ABS(Diff) < 1.0` *unit* AND
  `ABS(Diff)*Price < 5` in the asset's currency. `CASH` rows: `ABS(Diff) < 5` *units of
  the currency itself*. Defaults; the user can widen, never silently. Integer-sized
  asset diffs and material cash diffs are presumed-real and reported, never absorbed.
- **Shape follows kind.** `Asset ≠ Currency` → `BUY` / `SELL` with `ValueGross = ABS(Diff)*Price`
  and `Value = 0` (no cash leg). `Asset = Currency` → `GENERAL LEDGER RECEIPT` / `GENERAL
  LEDGER DELIVERY` with `Value = Quantity = ValueGross = ABS(Diff)` (full cash impact).
  Never use `ASSET RECEIPT` / `ASSET DELIVERY` here — those are for custody-to-custody
  in-kind transfers, not for reconciliation plugs.
- **Side is dictated by the sign of `Diff`.** `Diff > 0` (custody > ours) → `BUY` (asset)
  / `GENERAL LEDGER RECEIPT` (cash). `Diff < 0` (custody < ours) → `SELL` (asset) /
  `GENERAL LEDGER DELIVERY` (cash). Never invent the other direction.
- **`AssetRelated` rule.** On a `BUY`/`SELL` with `Status = 'UPDATED'`, `AssetRelated =
  Asset` is required by the procedure. On a `GENERAL LEDGER RECEIPT`/`DELIVERY`
  reconciliation plug, `AssetRelated` must be **`NULL`** — there is no originating security.
- **Lock-gate before write.** A dust row whose date is on/before the active CheckedDate is
  reported, not written, and **the lock is not moved from this skill**.
- **Drop `AccountCurrency` and `AccountFx`** from the params; pass **absolute** values; set
  `Obs` (`'automatic quantity adjustment'` for ASSET rows, `'automatic cash adjustment'`
  for CASH rows) and `AgentCheck` with the `[PQA]` tag on every row.
- **Preview before batch, verify after.** Re-run the §1 diff and re-SELECT each inserted
  pk; nothing absorbed silently.
- **Reply in the user's language** (PT/EN) and echo the resolved scope.

## When unsure

- **Diff is small but exactly 1.0 (or 2.0, 5.0, …)** → almost certainly a real trade missing
  or doubled; route to the loader / `duplicate-trade-reconcile`, do not absorb.
- **`CASH` diff is small but a clean round number** (10 / 50 / 100 / 1000 of the currency)
  → almost certainly a missing `DEPOSIT`/`WITHDRAW` or an unbooked fee — investigate
  through the custody's transaction feed before considering a GL plug.
- **Diff equals `QuantityLending` or `QuantityMargin`** (per `position/reconciliation`) →
  not dust; custody reports only the unencumbered portion. Report.
- **The asset is unpriced on the trade date** → no `ValueGross` can be computed without
  inventing a price. Report and stop for that asset; price it via the asset domain first.
- **The active lock is already at the reconciliation date** → too late for this run.
  Either the user wants to recoil the lock (their decision, audited, via `CheckedDate`
  procedure — out of this skill) or they accept the residue. Default: report and stop.
- **Multiple custody rows for the same `(Date, Account, AssetR)`** (multiple feeds) →
  pick the canonical feed for the account first (`Global.ClientAccount.OfficialFeed`);
  otherwise the diff is meaningless. Report.
