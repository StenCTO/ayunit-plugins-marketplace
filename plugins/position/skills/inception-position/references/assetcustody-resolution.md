# §2a — Resolve untranslated `AssetR` rows in-flow

Read this **only when §1e-B counted `n_unresolved > 0`** — i.e. the holdings come from
`v_CustodyPosition` and one or more rows still have `Asset IS NULL` (the custodian sent a
`TickerCustody` that `Portfolio.AssetCustody` doesn't yet map to a canonical `Global.Asset`).
If every holding already resolves to an `Asset`, skip this entirely and go to §3.

This sub-flow closes the loop in-MCP — no SSMS hand-off. Two procedures make it possible:
`Portfolio.AssetCustody_Update @CMD='I'` (insert a `TickerCustody → Asset` mapping) and
`Portfolio.CustodyPosition_Update @CMD='Update_Missing_Asset'` (back-fill `fk_AssetID` on
historical rows once the mapping exists). Both are WRITES — preview and confirm before calling.

Distinct from §1e-A: here the asset already exists in `Global.Asset`; only the custody-side
mapping is missing. If the resolution loop below can't find a `Global.Asset` for a given
`AssetR`, *then* it routes to the `asset-register` skill (the 1e-A path).

## i. Detect the untranslated holdings (the ones counted in §1e-B)

```sql
SELECT pk_CustodyPositionID, AssetR, IsinR, AnbimaCodeR, Custody,
       Quantity, Value, ValueAfterTaxes
FROM Portfolio.v_CustodyPosition
WHERE Account = '<account>' AND Date = '<inception_date>' AND Asset IS NULL
ORDER BY AssetR;
```

## ii. For each row, attempt to resolve `AssetR` → canonical `Global.Asset`

Try the identifier columns in order of specificity (one ANY-of OR-lookup). This mirrors the
proc's own `PI`-branch resolution, which matches `@Asset` against
`Asset / BbgCode / Isin / Cusip / AnbimaCode / FundCode / ClassCode / Cnpj / ExchangeCode`:

```sql
SELECT TOP 5 Asset, Description, AssetGroup, SecurityType, Product, AssetClass, Activated
FROM Global.v_Asset
WHERE Asset = '<AssetR>'          -- exact code match (frequently true for BR fixed income)
   OR Isin  = '<IsinR>'           -- if IsinR is populated
   OR AnbimaCode = '<AnbimaCodeR>'-- if AnbimaCodeR is populated
   OR Cnpj  = '<AssetR>';         -- if AssetR looks like a 14-digit CNPJ (fund tickers)
```

Bucket each row by the result:

- **resolved → 1 candidate, `Activated = 1`** → queue the proposed `AssetCustody_Update I`.
- **resolved → multiple candidates** → ambiguity; ask the user to pick (don't silently take
  the first row). Consult [`reconciliation`](ayunit://docs/position/reconciliation) for the
  fallback order.
- **unresolved → 0 candidates** → the asset itself isn't registered; hand off to the
  **`asset-register`** skill, register it, then come back to step (ii) for this `AssetR`.

## iii. Preview the proposed `AssetCustody` mappings as a table

One row per `AssetR` to insert, then wait for explicit confirm:

| AssetR | Custody | → Asset | Description | PositionFactor | PriceFactor | Note |
|---|---|---|---|---:|---:|---|
| `766378` | BTG | `XPML11` | XP Malls FII | `1` | `1` | matched on `Asset` exact |
| `…`     | …   | …       | …            | …  | …  | matched on `Isin` |

**Defaults are `PositionFactor = 1` and `PriceFactor = 1`.** Ask the user explicitly if the
custody scales differently — common cases: bonds priced as %-of-face (`PriceFactor = 0.01`),
quantities reported in lots, or custodian-specific conventions. Don't assume.

## iv. For each confirmed mapping, call `execute_procedure`

```
execute_procedure(
  database  = "AgnesOrg00DB",
  procedure = "Portfolio.AssetCustody_Update",
  cmd       = "I",
  params    = {
    "Asset":          "<resolved_asset>",
    "Custody":        "<custody>",
    "TickerCustody":  "<AssetR>",
    "PositionFactor": 1,                 # override only if the custody scales
    "PriceFactor":    1,
    # optional: "TickerCustody2": "<IsinR>", "DescriptionCustody": "<…>"
  }
)
```

Capture the returned `pk_AssetCustodyID` for each. A failure (e.g. `@Asset` not found in
`Global.Asset` despite step (ii) saying otherwise) means the `Activated` flag flipped between
step (ii) and step (iv) — re-resolve and retry that single row, don't proceed.

## v. Back-fill the historical `CustodyPosition` rows, scoped narrowly

```
execute_procedure(
  database  = "AgnesOrg00DB",
  procedure = "Portfolio.CustodyPosition_Update",
  cmd       = "Update_Missing_Asset",
  params    = {
    "Date":    "<inception_date>",
    "Account": "<account>",
    "Custody": "<custody>"
  }
)
```

**Never run this unscoped.** Without all three filters the proc back-fills every previously-
untranslated `CustodyPosition` row across the entire firm, silently mutating accounts and
dates outside the inception scope. This is the single most dangerous call in the skill — the
read-only-sounding name hides a firm-wide WRITE.

The returned rowset lists every `pk_CustodyPositionID` it just back-filled with the matched
`Asset` and `Status='Updated'` — show it to the user as confirmation.

## vi. Verify this sub-flow closed the loop

```sql
SELECT COUNT(*) AS n_still_unresolved
FROM Portfolio.v_CustodyPosition
WHERE Account = '<account>' AND Date = '<inception_date>' AND Asset IS NULL;
```

Required: `0`. Any non-zero result means a row didn't back-fill — usually a `Custody` value
mismatch between `v_CustodyPosition` and `v_AssetCustody`, or a mapping that landed with
`fk_AssetID = NULL` (the resolution from step (ii) was stale). Stop and investigate before
returning to §3 — **never** continue with `Asset IS NULL` rows still present.
