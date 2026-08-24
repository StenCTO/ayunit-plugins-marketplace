# Seed-build conventions for BTG-onshore inception

Codified from the XP onshore inception run (2026-08-19; 75 accounts,
R$1.05bn seeded), extended with BTG-specific adaptations. This file is the
**source of truth** for every column value in the inception seed rows the
orchestrator INSERTs via `Portfolio.AccountPosition_Update @CMD='I'`.

Cross-references:
- [`ayunit://docs/position/inception`](ayunit://docs/position/inception) —
  the general inception recipe (defaults, gates, cross-currency, cash/FX).
  This file **extends** it with BTG per-lot conventions; the invariants
  there still apply.
- `position:inception-position` `SKILL.md` §3 (defaults), §4 (sanity gates),
  §6-7 (canary/batch), §8 (verify + reconcile) — the invariants the
  orchestrator's Step 3.6 INSERT loop mirrors verbatim.
- `account-transaction:references/write-invariants.md` — sign conventions,
  account key format, forbidden fields.

---

## 1. Grain and sourcing

- **Grain:** one seed row per `(Date=<cutoff_date>, Account, Asset)`.
- **`Quantity` sourcing:** `SUM` over the account's `v_CustodyPosition`
  rows for that `(Account, Asset, cutoff)` — BTG can emit multiple rows
  for the same asset (e.g. same fund across sub-books). Aggregate on the
  distinct `pk_CustodyPositionID` grain:

  ```sql
  SELECT Asset,
         SUM(Quantity) AS Qty,
         SUM(Value)    AS CustValue,
         SUM(ValueAfterTaxes) AS CustValueAfterTaxes,
         SUM(CASE WHEN Value IS NOT NULL AND ValueAfterTaxes IS NOT NULL
                  THEN Value - ValueAfterTaxes ELSE 0 END) AS Provision
  FROM Portfolio.v_CustodyPosition
  WHERE Account   = '<account>' AND Custody = '<custody>'
    AND CAST([Date] AS date) = '<cutoff_date>' AND Asset IS NOT NULL
  GROUP BY Asset;
  ```

- **`Price` sourcing:** `AssetData.v_Price` on `<cutoff_date>`, TOP-1
  preferring the asset's designated `Global.Asset.Source` when >1 source
  publishes the same date:

  ```sql
  WITH src AS (
    SELECT a.Asset, a.Source AS PreferredSource
    FROM Global.v_Asset a WHERE a.Asset = '<asset>'
  ),
  price_pick AS (
    SELECT TOP 1 p.Price, p.Source, p.[Date] AS PriceDate
    FROM AssetData.v_Price p
    LEFT JOIN src ON src.Asset = p.Asset
    WHERE p.Asset = '<asset>' AND CAST(p.[Date] AS date) = '<cutoff_date>'
    ORDER BY CASE WHEN p.Source = src.PreferredSource THEN 0 ELSE 1 END,
             p.Source
  )
  SELECT Price, Source AS PriceSource, PriceDate FROM price_pick;
  ```

  No row → the asset has no price on cutoff; block the account with
  `status = "blocked_no_price"` (per SKILL.md §Step 3.3.3).

- **`ValueClose`:** `Quantity × Price × ContractSize` (from `Global.Asset`).
  This is what the seed writes. Any gap to the custody's `Value` is a
  price-source difference (BTG's price ≠ ours) — expected on many funds
  and captured by the reconcile.

- **`ValueCloseAfterTaxes` (VAT):** `Value − provision`, where
  `provision = MAX(0, Custody.Value − Custody.ValueAfterTaxes)`. Clamp
  negative to 0 (a negative provision is data corruption in custody;
  don't propagate it). If custody's `ValueAfterTaxes = 0` (no tax
  reserved), `provision = 0` → `VAT = Value`.

---

## 2. Column defaults (every row)

Per [`ayunit://docs/position/inception`](ayunit://docs/position/inception)
§Column-by-column defaults, extended for BTG:

| Column | Value | Note |
|---|---|---|
| `Date` | `<cutoff_date>` | The inception snapshot. |
| `Account` | `<account>` | 9-digit zero-padded (BTG onshore convention). |
| `Custody` | `<custody_name>` | Exact string from `Global.Custody` (verified in SKILL §Step 1.2). |
| `Currency` | Asset's currency from `Global.Asset` | Not the account's currency. |
| `Asset` | The canonical Asset code | Must be registered in `Global.Asset`; the proc resolves `@Asset → fk_AssetID`. |
| `QuantityClose` | `Qty` from §1 | Positive on longs, negative on repo/CPR/short. |
| `QuantityOpen` | = `QuantityClose` | Steady-state seed. |
| `QuantityTransaction` | `0` | No flows on the seed. |
| `QuantityLending` | `0` | Override only if custody splits it (subset of Close). |
| `QuantityMargin` | `0` | Same. |
| `PriceClose` | `Price` from §1 | |
| `PriceOpen` | = `PriceClose` | |
| `AvgPrice` | Depends on asset kind (§3) | Only chance to set cost basis; the pipeline updates it only from BUYs after `D+1`. |
| `PriceSourceClose` | The `Source` from §1's price pick | Must exist in `Global.PriceSource`. |
| `PriceSourceOpen` | = `PriceSourceClose` | |
| `PriceDateClose` | The `PriceDate` from §1 (usually = `cutoff_date`) | |
| `PriceDateOpen` | = `PriceDateClose` | |
| `ValueClose` | `Qty × Price × ContractSize` | Identity gate §4. |
| `ValueOpen` | = `ValueClose` | |
| `ValueCloseAfterTaxes` | `Value − provision` (§1) | |
| `ValueOpenAfterTaxes` | = `ValueCloseAfterTaxes` | |
| `ValueTransaction` | `0` | |
| `SellIncomeTaxes` | `0` | |
| `PnlExGrossUp` | `0` | |
| `PnlGrossUp` | `0` | |
| `PnlDaily` | `0` | |
| `DailyReturn` | `0` | |
| `Mtd` | `0` | |
| `Ytd` | `0` | |
| `Itd` | Depends on asset kind (§3) | |
| `AcquisitionDate` | Depends on asset kind (§3) | |
| `Activated` | `1` (or omit — not in the `I` INSERT column list) | Not persisted from param on `I`; per `position:inception-position` "What the procedure actually persists". |
| `AccountCurrency` / `AccountFx` | **DO NOT PASS** | Recomputed server-side; the MCP wrapper rejects payloads with either field. |

---

## 3. Per-asset-kind conventions

Map each held asset to its **kind** (from `Global.Asset.AssetGroup` +
`SecurityType` + the BTG feed section — see
[`btg-feed-shape.md`](btg-feed-shape.md)) and apply the kind's recipe.

### 3.1 Funds (Fundos / FII / FIDC / FIP / Previdência — any BR fund)

BTG feed carries `accY` (accumulated yield since acquisition, expressed
as a multiplier `> 1` for a positive return) and `originalDate` per lot.

- `AvgPrice = Price / accY`
- `Itd     = accY − 1`
- `AcquisitionDate = originalDate` (BTG dummy `0001-01-01` →
  `AcquisitionDate = cutoff_date`)

**Invalid `accY` handling:** if `accY IS NULL OR accY <= 0`:
- `Itd = 0`
- `AvgPrice = Price` (no cost basis known; the pipeline will update on
  the next BUY)
- `AcquisitionDate = cutoff_date`

Flag the row as `accY_invalid: true` in the ledger so the analyst can
back-fill cost basis manually if needed.

### 3.2 Pension (Previdência — VGBL / PGBL certificates)

BTG emits **one row per certificate** for the same fund CNPJ (each
certificate = a distinct application). The seed grain is `(Account,
Asset)`, so combine per fund CNPJ:

- `Quantity = SUM(certificate.quantity)`
- `accY_combined = SUM(qty × accY) / SUM(qty)` (quantity-weighted)
- `AcquisitionDate = MIN(certificate.originalDate)`
- Then apply §3.1 fund formulas on the combined `accY_combined`.

### 3.3 Traded funds / equities / ETFs (ticker-matched)

- Match asset by ticker → `Global.Asset.Bbg` (or `ExchangeCode`).
- `Itd = 0` (no accumulated-yield concept from BTG for these).
- `AvgPrice = Price` (use `purchaseDate`-anchored cost basis if BTG
  supplies it — but usually not).
- `AcquisitionDate = purchaseDate` if BTG supplies it, else
  `cutoff_date`.

### 3.4 Fixed income (CDB / LCI / LCA / debêntures / CRI / CRA / bonds)

- **Match order** (first hit wins):
  1. BTG's internal code → `TickerCustody` in `v_AssetCustody` (numeric
     `assetCode`).
  2. ISIN → `Global.Asset.Isin`.
  3. AnbimaCode → `Global.Asset.AnbimaCode`.
  4. Maturity + quantity (last resort — same-CETIP NTN-Bs use this).
- **Same-CETIP NTN-B discriminator:** two NTN-Bs sharing a CETIP code
  but with different maturities (e.g. NTN-B 2035 vs NTN-B 2045) must be
  discriminated by `Maturity` — the CETIP alone will match both.
- **Combine secondary + Tesouro Direto lots** of the same instrument:
  a Treasury bond may sit in both books (retail-side Tesouro Direto +
  secondary-market lot). Combine into one seed row per instrument:
  - `Quantity = SUM(...)` across lots
  - `Price = Price` (from `v_Price`; not lot-averaged)
  - `AvgPrice = Price`
  - `AcquisitionDate = MIN(lot.purchaseDate)` (earliest of the combined
    lots)
  - Log the combined lot count in `account.fi_lot_combines` for the
    ledger.
- `Itd = 0` (no accumulated-yield concept for FI seeds).
- `AvgPrice = Price` (FI cost basis at inception = seed price; the
  pipeline updates on future BUYs only).

### 3.5 COE (Certificado de Operações Estruturadas)

Same treatment as §3.4 fixed income: `Itd = 0`, `AvgPrice = Price`,
`AcquisitionDate = purchaseDate` (else `cutoff_date`), match by
BTG code → ISIN → maturity + qty.

### 3.6 Treasury (Tesouro Direto — bare bones)

Match by ticker (`Global.Asset.Bbg` — e.g. `NTN-B 2035`), else by ISIN.
Combine with any secondary-market lot per §3.4. `Itd = 0`, `AvgPrice =
Price`, `AcquisitionDate = purchaseDate` (else `cutoff_date`).

### 3.7 Repo (compromissada — BTG `repo` section)

BTG emits repos as a cash-collateralised deposit at par. Model as a
`COMPROMISSADA` position:

- `Asset` = the compromissada Asset code (usually `COMPROMISSADA_BRL`
  or a per-issuer variant — check `Global.Asset` for the canonical
  code your book uses).
- `Quantity = Value` (yes — the seed grain is Value; BTG's `quantity`
  field is either 1 or absent).
- `Price = 1` (`Price × Qty = Value` identity).
- `AvgPrice = 1`.
- `Itd = 0`.
- `AcquisitionDate = cutoff_date`.

### 3.8 Cash (BRL, USD, or any other currency the account holds)

- `Asset` = the currency code (`BRL`, `USD`, etc.). Must exist in
  `Global.Asset` with `AssetGroup = 'Cash'`.
- `Quantity = Value` (Value in the account's currency; BTG's cash rows
  are always in BRL for onshore).
- `Price = 1`.
- `AvgPrice = 1`.
- `Itd = 0`.
- `AcquisitionDate = cutoff_date`.

Per `position:inception-position` §3, the `AccountPosition_Update`
proc's `PI` (preview) branch forces `Price = 1` for
`AssetGroup = 'Cash'` — same convention.

### 3.9 Skip rules

Skip (do NOT INSERT) any custody row where **both**:

- `Quantity = 0` AND
- `Value = 0`

These are BTG "zombie" rows (accounts that briefly held the asset then
sold; BTG still emits a zero-line for reporting). Seeding them would
land a phantom row in `AccountPosition` that never rolls off cleanly.

---

## 4. Sanity gates (per row, before INSERT)

Fail the row (and stop the account with
`status = "seed_build_failed"`) on any:

- **Negative provision.** `provision < 0` (from §1). Never propagate.
- **VAT > Value.** `|ValueCloseAfterTaxes| > |ValueClose|`. Net > gross
  is impossible; suggests custody-side corruption or a wrong sign
  applied.
- **Missing AvgPrice on non-cash.** `AvgPrice IS NULL OR AvgPrice = 0`
  on any non-cash asset. Break cost-basis on the first future SELL.
- **Identity break.** `|ValueClose − Quantity × Price × ContractSize|
  > 0.01 × |ValueClose|` (1% tolerance for rounding/accrual). A 100×
  gap is a `ContractSize` issue on the asset registration — fix there,
  not here.
- **FI match count = 0.** A held fixed-income asset with no
  fact-transcription match from §Step 3.2 (safety net for feed
  completeness).
- **Sign mismatch.** `Quantity` sign doesn't match the position type
  (repos / CPRs / liabilities negative; longs positive). Per
  `account-transaction:references/write-invariants.md#sign-convention`.

Per-row failures accumulate in `account.seed_build_errors`; ANY failure
skips the seed INSERT for the whole account.

---

## 5. Minimal param set for `AccountPosition_Update @CMD='I'`

Per `position:inception-position` §6, use the minimal-param canary shape.
The persisted-column subset on `I` excludes several fields the view
re-derives (`Product`, `AssetClass`, `AssetGroup`, `Client`,
`AccountCurrency`, `AccountFx`, `Activated`), so passing them is a
harmless no-op — but the minimal set below is the honest contract:

```
execute_procedure(
  database  = "AgnesOrg00DB",
  procedure = "Portfolio.AccountPosition_Update",
  cmd       = "I",
  params    = {
    "Date":             "<cutoff_date>",
    "Account":          "<account>",
    "Custody":          "<custody_name>",
    "Asset":            "<asset>",
    "Currency":         "<asset_ccy>",
    "AcquisitionDate":  "<acq_date>",
    "QuantityOpen":     <qty>, "QuantityClose": <qty>,
    "QuantityTransaction": 0,
    "QuantityLending":  0, "QuantityMargin": 0,
    "PriceSourceOpen":  "<src>", "PriceSourceClose": "<src>",
    "PriceDateOpen":    "<pdate>", "PriceDateClose": "<pdate>",
    "AvgPrice":         <avg>, "PriceOpen": <price>, "PriceClose": <price>,
    "ValueTransaction": 0,
    "ValueOpen":        <value>, "ValueClose": <value>,
    "ValueOpenAfterTaxes":  <vat>, "ValueCloseAfterTaxes": <vat>,
    "SellIncomeTaxes":  0,
    "PnlExGrossUp":     0, "PnlGrossUp": 0,
    "PnlDaily":         0, "DailyReturn": 0,
    "Mtd":              0, "Ytd":         0, "Itd": <itd>
  }
)
```

A success returns `status = "success"` with `rowcount = -1` and **no
`pk`** — re-SELECT `v_AccountPosition` to confirm the row landed and
capture its `pk_AccountPositionID` for the ledger.

---

## 6. Real-world sanity checks (post-seed, per account)

After the full account is seeded, before the reconcile in SKILL §Step 3.7:

- `SELECT COUNT(*)` from `v_AccountPosition` and from
  `Portfolio.AccountPosition` (base table) both equal the batch size.
  A mismatch = orphan NULL-FK row on the base table (view hides it);
  roll back and investigate.
- `AccountFx` on cross-currency rows is sensible (BTG onshore is
  BRL-only for `AccountCurrency`, so `AccountFx = 1.0` on every row;
  anything else is a bug in the price history for the asset's
  currency).
- Every row: `QuantityOpen = QuantityClose`, `ValueTransaction = 0`,
  `PnlDaily = 0`, `DailyReturn = 0`.

Only then run the SKILL §Step 3.7 reconcile against `v_CustodyPosition`.
