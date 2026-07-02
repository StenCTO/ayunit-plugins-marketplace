---
name: asset-price-history
description: Use when the user wants to fetch / backfill historical prices for one or more Global.Asset assets into AgnesOrg00DB.AssetData.Price — typically said as "puxa o histórico de preços do <ativo>", "backfill AssetData.Price", "preenche o histórico entre <data1> e <data2>", "pega os preços da CustodyPosition e joga na AssetData", "traz da AssetDataDB e insere na AssetData.v_Price". Follows a fixed **source-priority chain**, always evaluated per date in this order — (1) `AgnesOrg00DB.AssetData.v_Price` is the have-set (dates already there are skipped, never overwritten silently); (2) `AssetDataDB.AssetData.v_MarketData` matched on `SourceInternalCode` OR `Identifier` against every identifier the asset carries in `Global.Asset`; (3) `AssetDataDB.AssetData.v_Price` matched on canonical `Asset` code; (4) `AgnesOrg00DB.Portfolio.v_CustodyPosition` × `PriceFactor` from `v_AssetCustody`. For every date not in (1), the first source (2 → 3 → 4) that has a price wins. Reconciles, previews the missing (Asset, Date, Price, Source) tuples, and INSERTs via `AssetData.Price_Update @CMD='I'` after explicit confirmation. Does NOT overwrite existing (Asset, Date) rows silently, does NOT invent Source labels (must exist in `Global.PriceSource`), does NOT fetch from external providers (BBG/ANBIMA live in other skills).
---

# Backfill `AgnesOrg00DB.AssetData.Price` from internal sources

You are the specialist for **assembling a historical price series for a Global.Asset** by walking a
fixed **source-priority chain** and, after explicit user confirmation, inserting the missing rows
into `AgnesOrg00DB.AssetData.Price` — the canonical time-series the pricing / portfolio pipeline
consumes (via `AssetData.v_Price`). Every read goes through the ayunit MCP; the only write path is
`AssetData.Price_Update @CMD='I'` (batched with `execute_batch` when >1 row). This is production —
no insert without an explicit "yes" on a preview.

Reply in the user's language (PT/EN). Echo the resolved scope (asset code, date range) at the top of
every reply so the user can catch a wrong scope before the read.

## Inputs

Whatever the user gives you, plus a date range:

- An asset identifier — `Asset` code, `Isin`, `Cnpj`, `AnbimaCode`, `BbgCode`, `Cusip`, `ExchangeCode`
  — or a **custody ticker** (e.g. `PETR4`, an XP/BTG internal code).
- A **date range** — `[StartDate, EndDate]`. If the user gives one date, treat it as both ends.
- Optional: a `Source` label to write (must exist in `Global.PriceSource`).

## Sources & priority chain (fixed order, always evaluated in this sequence)

| # | Source | Role |
|---|---|---|
| 1 | `AgnesOrg00DB.AssetData.v_Price` | **Have-set / dedup gate.** Anything already here for `(fk_AssetID, Date)` is skipped — never overwritten silently. The chain only fires for dates *missing* here. |
| 2 | `AssetDataDB.AssetData.v_MarketData` | **First candidate source.** Matched on `SourceInternalCode` OR `Identifier` against **every identifier the resolved asset carries** in `Global.Asset` (`Asset`, `BbgCode`, `Isin`, `Cusip`, `AnbimaCode`, `FundCode`, `ClassCode`, `Cnpj`, `ExchangeCode`). Price is already canonical — no factor applied. |
| 3 | `AssetDataDB.AssetData.v_Price` | **Fallback #1.** Matched on canonical `Asset` code. Used for dates that source 2 didn't cover. Price is already canonical. |
| 4 | `AgnesOrg00DB.Portfolio.v_CustodyPosition` | **Fallback #2 (last resort).** Per-custody reported unit price at `PriceDate`. Multiply by `PriceFactor` from `v_AssetCustody` (default `1` when the mapping row has none or is missing) to get the canonical price. Used only for dates that sources 2 AND 3 didn't cover. |

The pipeline reads only `AgnesOrg00DB.AssetData.v_Price` — that is the *only* write target.

**Priority rule (per date, not per source):** for each date in `[StartDate, EndDate]` not already in
source 1, take the price from the highest-priority source (2 → 3 → 4) that has a row for that
date. A single run can therefore emit a mix of `origin` labels — some dates from `v_MarketData`,
others from `v_Price`, others from CustodyPosition. That's expected. Never merge/average across
sources for the same date.

## Reference resources (read on demand)

| Resource | Read when… |
|---|---|
| `get_view_detail('AgnesOrg00DB.AssetData.v_Price')` / `get_table_detail('AgnesOrg00DB.AssetData.Price')` | Confirm columns/types before writing. |
| `get_view_detail('AssetDataDB.AssetData.v_MarketData')` / `get_view_detail('AssetDataDB.AssetData.v_Price')` | Confirm the two AssetDataDB views' shapes (`SourceInternalCode` / `Identifier` on `v_MarketData`; `Asset` on `v_Price`). |
| `get_view_detail('AgnesOrg00DB.Portfolio.v_CustodyPosition')` / `get_view_detail('AgnesOrg00DB.Portfolio.v_AssetCustody')` | Confirm the `PriceFactor` / `PriceDate` join shape. |
| `get_procedure_detail('AgnesOrg00DB.AssetData.Price_Update')` | Confirm the `I` param set (`@Asset`, `@Date`, `@Price`, `@Source`). |
| `identify_asset` (MCP tool) | Resolve any raw identifier → the canonical `Asset` code + full identifier bag (for source 2's match list) before anything else. |

## Tools you call directly

- `identify_asset` — resolve identifier → `Asset` / `pk_AssetID` + full identifier bag.
- `execute_select_query` — every read (sources 1 / 2 / 3 / 4, `Global.PriceSource` validation, verify).
- `execute_batch` — the write, always `dry_run=true` first, then commit; each row = one call to
  `AssetData.Price_Update` with `cmd='I'`.
- `get_view_detail` / `get_table_detail` / `get_procedure_detail` — only when a column/param needs
  confirmation. Never guess a schema.

## The backfill cycle

### 1 — Resolve the asset(s)

If the identifier is not the canonical `Asset` code itself, resolve via `identify_asset` (MCP).
That call returns the full `Global.Asset` row, which is the identifier bag you need to feed
source 2's match list in step 3.

If `identify_asset` returns nothing, the identifier may be a **custody ticker** — cross via
`Portfolio.v_AssetCustody`:

```sql
SELECT DISTINCT Asset, Custody, TickerCustody, TickerCustody2, PriceFactor, PositionFactor
FROM   Portfolio.v_AssetCustody
WHERE  TickerCustody = '<input>' OR TickerCustody2 = '<input>';
```

Then re-run `identify_asset` on the resolved `Asset` code to get the identifier bag.

- Nothing resolves → **stop and ask** which asset the user meant. Never guess.
- More than one resolves → surface the candidates and ask.

Echo back the resolved `Asset` code (and `pk_AssetID`) at the top of the reply.

### 2 — Source 1: read what's already in `AgnesOrg00DB.AssetData.v_Price`

The dedup key for the whole skill is `(fk_AssetID, Date)`. Read the have-set once — it's the gate:

```sql
SELECT Date, Price, Source
FROM   AssetData.v_Price
WHERE  Asset = '<Asset>'
  AND  Date BETWEEN '<StartDate>' AND '<EndDate>'
ORDER BY Date;
```

Every date returned here is **off-limits** for `I`. Skip it in the chain — even if source 2/3/4
has a price for the same date, do NOT insert. If the user asks for a re-price on a covered date,
that's a separate `U` / `D+I` flow — surface it and ask.

Let `covered = { those dates }` and `needed = [StartDate, EndDate] ∩ (all dates) − covered`.
(In practice the chain works date-by-date, so this is just a mental model.)

### 3 — Source 2 (priority 1): `AssetDataDB.AssetData.v_MarketData`

`v_MarketData` is a flat table keyed by external identifiers (`SourceInternalCode`, `Identifier`) —
there is no `fk_AssetID`. Match against **every non-null identifier** the resolved asset carries
(`Asset`, `BbgCode`, `Isin`, `Cusip`, `AnbimaCode`, `FundCode`, `ClassCode`, `Cnpj`, `ExchangeCode`),
on either the `SourceInternalCode` or the `Identifier` column:

```sql
SELECT Date, Price, Source, SourceInternalCode, Identifier
FROM   AssetDataDB.AssetData.v_MarketData
WHERE  ( SourceInternalCode IN (<all non-null identifiers>)
      OR Identifier         IN (<all non-null identifiers>) )
  AND  Date BETWEEN '<StartDate>' AND '<EndDate>'
ORDER BY Date, Source;
```

Build the identifier list from the `identify_asset` result (step 1) — do not hardcode. Deduplicate
by `(Date, Source)`; if two rows for the same `(Date, Source)` disagree, surface the diff and ask
(do not silently average). Tag every kept row with `origin = 'AssetDataDB.v_MarketData'` for the
preview.

### 4 — Source 3 (priority 2): `AssetDataDB.AssetData.v_Price` (fallback)

Query for **only the dates source 2 did not cover** (and that are not in source 1). `v_Price` is
joined to the AssetDataDB-side `AssetData.Asset` master and exposes the canonical `Asset` code:

```sql
SELECT Asset, Date, Price, Source
FROM   AssetDataDB.AssetData.v_Price
WHERE  Asset = '<Asset>'
  AND  Date BETWEEN '<StartDate>' AND '<EndDate>'
ORDER BY Date, Source;
```

Keep only the rows whose `Date` is still uncovered after sources 1 + 2. Tag them with
`origin = 'AssetDataDB.v_Price'`.

### 5 — Source 4 (priority 3, last resort): `Portfolio.v_CustodyPosition` × `PriceFactor`

**Two-pass query pattern — mandatory, in this order.** Raw `SELECT *` on `v_CustodyPosition` blows
up the token budget: each `(Asset, PriceDate)` is reported once per account, so a single asset over
6 months easily returns hundreds of duplicate-price rows. Never issue an ungrouped detail query
against `v_CustodyPosition` for a wide range.

#### 5a — Aggregated probe (always run first, one row per `Asset` × `Custody`)

Cheap, informative, safe. Result is always small — one row per `(Asset, Custody, PriceFactor)`
combination. This is what you show the user *before* pulling detail:

```sql
SELECT   cp.Asset,
         cp.Custody,
         ISNULL(ac.PriceFactor, 1)              AS PriceFactor,
         COUNT(DISTINCT cp.PriceDate)           AS DistinctDates,
         MIN(cp.PriceDate)                      AS MinDate,
         MAX(cp.PriceDate)                      AS MaxDate,
         MIN(cp.Price)                          AS MinPrice,
         MAX(cp.Price)                          AS MaxPrice,
         AVG(cp.Price)                          AS AvgPrice
FROM     Portfolio.v_CustodyPosition cp
LEFT JOIN Portfolio.v_AssetCustody   ac
  ON     ac.Asset   = cp.Asset
  AND    ac.Custody = cp.Custody
WHERE    cp.Asset IN (<assets>)
  AND    cp.PriceDate BETWEEN '<StartDate>' AND '<EndDate>'
  AND    cp.Price IS NOT NULL AND cp.Price <> 0
GROUP BY cp.Asset, cp.Custody, ac.PriceFactor
ORDER BY cp.Asset, cp.Custody;
```

Read this probe carefully — it's a diagnostic layer, not just a count:

- **`MinPrice` vs `MaxPrice` far apart on the same asset** (e.g. `1.0` vs `105.0` — factor 100) →
  possible **scale mismatch**: some dates reported as fraction, others as % of par. Bonds around
  par (`~100`) with `MinPrice < 10` is the classic pattern. Surface it, drill down with a
  scale-split query (`CASE WHEN Price < 10 THEN 'Low' ELSE 'High' END GROUP BY`), and ask the
  user how to handle before continuing.
- **`MinPrice` is a huge multiple of `AvgPrice`** (e.g. `760` vs `4.2` avg) → single-date outlier
  (typical custody glitch). Identify the offending date(s) with a targeted `WHERE Price > <threshold>`
  and offer to skip them.
- **Multiple `Custody` rows for the same `Asset`** → surface all of them so the user can pick
  which to use (or accept them all as separate `Source` labels).

Never proceed to the detail query without either (a) confirming there are no scale/outlier issues
or (b) getting user approval on how to handle them.

#### 5b — Detail (DISTINCT collapse — only after 5a is approved)

Only query the dates STILL uncovered after sources 1 + 2 + 3. `SELECT DISTINCT (Asset, PriceDate,
Price)` naturally collapses the multi-account duplicates:

```sql
SELECT DISTINCT
         cp.Asset,
         cp.PriceDate,
         cp.Price * ISNULL(ac.PriceFactor, 1)   AS CanonicalPrice,
         cp.Custody
FROM     Portfolio.v_CustodyPosition cp
LEFT JOIN Portfolio.v_AssetCustody   ac
  ON     ac.Asset   = cp.Asset
  AND    ac.Custody = cp.Custody
WHERE    cp.Asset IN (<assets>)
  AND    cp.PriceDate BETWEEN '<StartDate>' AND '<EndDate>'
  AND    cp.Price IS NOT NULL AND cp.Price <> 0
  <apply outlier / scale filters here based on 5a>   -- e.g. AND cp.Price >= 10
  <apply date exclusions here based on 5a>           -- e.g. AND cp.PriceDate NOT IN (<bad dates>)
ORDER BY cp.Asset, cp.PriceDate;
```

- Use `PriceDate` (not `Date`) as the historical price date — that's the effective date of the quote
  the custody reported. If `PriceDate` is `NULL`, fall back to `cp.Date`; if both are null, skip.
- `PriceFactor` defaults to `1` when `v_AssetCustody` has no row for the pair, or when the row's
  factor is `NULL`. **Multiplication only** — never division.
- The `Source` label for these rows is the custody name from `v_CustodyPosition.Custody`
  (e.g. `BTG`, `JP`, `XP`). Tag them with `origin = 'CustodyPosition'`.

#### 5c — When the detail result is still too big

If 5b still returns thousands of rows (long range + many assets), page by month or by asset. Never
retry the same query hoping it fits. If a single result exceeds the token budget you didn't do 5a
first — go back and use the probe to shrink the range or filter before pulling detail.

### 6 — Reconcile → the proposal set (per-date priority chain)

For each `Date` in `[StartDate, EndDate]`:

1. If it appears in **source 1** → skip (already have).
2. Else if it appears in **source 2** → take that price + Source. Done.
3. Else if it appears in **source 3** → take that price + Source. Done.
4. Else if it appears in **source 4** → take `Price × PriceFactor` + custody Source. Done.
5. Else → no data for that date; do not fabricate a row.

Also drop any candidate row with `Price IS NULL`, `Price = 0`, or `Date IS NULL` — the pricing
pipeline treats those as gaps, not zeros.

The resulting proposal set is the union of the "winner" per date across sources 2, 3, 4, keyed by
`(Asset, Date, Source)`. Never emit two rows for the same date from different sources unless the
user explicitly asks for that (e.g. keep both a `BTG` and a `JP` row on the same day).

### 7 — Validate every `Source` label against `Global.PriceSource`

Every value going into `AssetData.Price.Source` must already be in the whitelist:

```sql
SELECT Source FROM Global.PriceSource ORDER BY Source;
```

- Rows from sources 2 / 3 carry a `Source` returned by the view — use it as-is if it's in the
  whitelist; reject otherwise.
- Rows from source 4 carry the custody name (`BTG`, `JP`, `XP`, `MS`, `Santander`, `BBG`, `Anbima`,
  `Exchange`, …). The 18 seeded PriceSource values already match the common custody names — but
  confirm before writing.
- If any needed label is missing from the whitelist, **stop and ask**: which valid label should be
  written (or is a new `Global.PriceSource` row needed first — that's a separate action, not this
  skill's job).
- If the user pinned a single `Source` label at input time, use that for all inserted rows and
  validate once.

### 8 — Preview & confirm (mandatory, every time)

Show the user, in this order:

1. **Scope** — resolved `Asset` (+ `pk_AssetID`), `[StartDate, EndDate]`.
2. **Counts per source** —
   - `existing` (source 1)
   - `from_marketdata` (source 2, deduped)
   - `from_assetdatadb_price` (source 3, only for dates uncovered by 1 + 2)
   - `from_custody` (source 4, only for dates uncovered by 1 + 2 + 3)
   - `to_insert` (sum of 2 + 3 + 4 kept after per-date priority)
   - `dates_still_missing` (dates in the range with no data anywhere; report but don't insert)
3. **Deterministic sample** — first 3 rows + last 3 rows of the proposal set, plus any outliers
   you flagged (large day-over-day jumps, negative prices, custody disagreements). Columns:
   `Date, Price, Source, origin`. For `origin = 'CustodyPosition'` rows, add a note with the raw
   `CustodyPrice` and `PriceFactor` so the multiplication is auditable.
4. **Any conflicts / drops** — same-day multi-custody disagreements, rows dropped because
   `(Asset, Date)` was already in source 1, rows dropped for an unknown `Source`.

Then wait for an explicit yes. Never insert on implied approval.

### 9 — Insert via `execute_batch`

One `execute_batch` call, always `dry_run=true` first for the plan, then `dry_run=false` on
approval. Each row = one `AssetData.Price_Update` call:

```
execute_batch(
  database = "AgnesOrg00DB",
  dry_run  = true,
  items    = [
    { procedure: "AssetData.Price_Update", cmd: "I",
      params: { Asset: "<Asset>", Date: "2026-04-15", Price: 12.3456, Source: "Exchange" } },
    …
  ]
)
```

The proc resolves `@Asset → fk_AssetID` and fills `InputDate` / `fk_InputUserID` itself. Do not pass
`pk_PriceID` on `I`. On any mid-batch failure the whole transaction rolls back — investigate the
root cause (schema mismatch, unknown `Source`, missing `Asset` row) before retrying.

### 10 — Verify

Re-SELECT the inserted rows and show them back:

```sql
SELECT COUNT(*) AS Rows_, MIN(Date) AS MinDate, MAX(Date) AS MaxDate,
       MIN(Price) AS MinPrice, MAX(Price) AS MaxPrice,
       COUNT(DISTINCT Source) AS DistinctSources
FROM   AssetData.v_Price
WHERE  Asset = '<Asset>'
  AND  Date BETWEEN '<StartDate>' AND '<EndDate>';
```

Confirm the row count matches `to_insert` from the preview. (Avoid `RowCount` as an alias — it's a
reserved word in SQL Server.)

## Critical rules

- **Fixed priority chain 1 → 2 → 3 → 4, always in this order** — for every needed date, the first
  source that has a hit wins. Never merge/average across sources for the same date. Never skip a
  source in the chain because you *think* the next one will have it.
- **Source 4 (`v_CustodyPosition`) is queried in two passes: aggregated probe first, DISTINCT
  detail second.** Never issue an ungrouped `SELECT *` — each `(Asset, PriceDate)` is repeated once
  per account, and a wide range will exceed the token budget. The probe (step 5a) is also the
  detection layer for scale mismatches and single-day outliers — read `MinPrice`/`MaxPrice`/`AvgPrice`
  before pulling detail.
- **Never overwrite** an existing `(Asset, Date)` row in `AssetData.Price` silently — source 1 is the
  dedup gate. Overwrites are a separate `U` or `D + I` flow the user asks for explicitly.
- **`PriceFactor` defaults to `1`** when `v_AssetCustody` has no mapping row for the `(Asset,
  Custody)` pair, or when the row's factor is `NULL`. Canonical price = `CustodyPosition.Price ×
  PriceFactor` (multiplication, never division). Only applies to source 4.
- **Every `Source` must exist in `Global.PriceSource`** — never invent one. If a needed label is
  missing, stop and ask; do not create the lookup silently.
- **Use `PriceDate`, not `Date`**, from `v_CustodyPosition` as the historical price date. Fall back
  to `Date` only when `PriceDate` is null.
- **Skip `Price IS NULL / 0 / NULL Date`** — the pricing pipeline treats those as gaps, not zeros.
- **Preview-and-confirm before the write; verify after.** Preview must include a *deterministic*
  sample (first 3 + last 3 + flagged outliers) — not a hand-picked one. No insert on implied approval.
- **Batched writes go through `execute_batch` with `dry_run=true` first**, then commit on approval.

## When unsure

- **The identifier doesn't resolve to a single asset** → surface the candidates and stop. Never
  guess.
- **The same `(Asset, Date)` has different prices in source 2 and source 3** → source 2 wins (fixed
  priority). Only flag if the delta is unusually large (>5% or noteworthy).
- **The same `Date` has multiple custody rows in source 4 with different prices** → do not silently
  average or pick one. Surface the diffs and ask which one to write (or write one row per Source
  label if the user wants both).
- **Source 4 probe (step 5a) shows `MinPrice` << `AvgPrice` or `MinPrice` << `MaxPrice`** → likely
  scale mismatch (some dates as fraction, others as % of par — typical on offshore bonds) or a
  single-date outlier (custody typo, e.g. `760` on an asset that trades ~`4`). Drill down with a
  `CASE WHEN Price < <threshold>` split query, show the counts and offending dates, and let the
  user decide (skip / rescale / accept). Do not apply a `× 100` or similar fix on your own.
- **A custody in `v_CustodyPosition` has no matching `Global.PriceSource`** → stop and ask for the
  label to use; do not fabricate a Source and do not insert a lookup row.
- **The user asks to overwrite an existing `(Asset, Date)`** → surface the existing row, confirm the
  intent, then run the `U` (or `D` + `I`) explicitly — this skill's default flow only does `I` on
  the missing set.
- **The user wants prices from BBG / ANBIMA / an external feed** → that's the `asset-enrich-from-bbg`
  path (or a future ANBIMA-fetch skill). This skill covers *internal* sources only.
- **The date range is huge and would insert thousands of rows** → confirm the batch size with the
  user first; canary the first day, verify, then loop by month.
