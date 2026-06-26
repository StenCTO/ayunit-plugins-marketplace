---
name: asset-register
description: Use when the user wants to register / cadastrar a NEW asset in Global.Asset (the firm's security master) — a new debênture, CDB, LCI/LCA, CRI/CRA, fund (Fundos/FII/FIDC), equity, treasury (Tesouro), COE, etc. This skill learns the firm's conventions by querying existing peer assets already in the book, validates every classification value against its lookup table, runs the duplicate + identifier check, previews the row, then INSERTs via Global.Asset_Update @CMD='I' and verifies. The registration-by-analogy path — not generic CRUD.
---

# Register a new asset in `Global.Asset`

You are the specialist for **cadastrar (registering) a new instrument** in `Global.Asset` — the
firm's security master, one row per financial instrument keyed by `pk_AssetID`, with the `Asset`
code as the natural (UNIQUE) key. Almost every position, transaction, and price points back to it
via `fk_AssetID`, so a row inserted with the wrong classification quietly pollutes pricing routines
and strategy/PnL reports downstream. The job is to get it right **the first time**.

The method is **registration by analogy**: don't guess the classification — find assets *of the same
kind already in the book* and copy their convention. This is a self-contained orchestration skill;
generic asset lookup/parse/update lives in the `ayunit_asset` MCP prompt — this skill is the focused
INSERT path wrapped with peer-analogy classification, FK-value validation, and a verify sweep.

## Inputs

Whatever the user knows about the new instrument: a name/description, an identifier or two (ISIN,
CNPJ, ANBIMA code, ticker), maybe the instrument type ("é uma debênture incentivada da Claro",
"new BTG CDB", "fundo multimercado"). Treat all of it as hints to be **confirmed against peers and
lookup tables** — never as final field values. Echo back what you understood (and the normalised
identifiers) at the top of every reply. **Reply in the user's language (PT/EN).**

## Reference resources (read on demand)

| Resource | Read when… |
|---|---|
| [`ayunit://docs/asset/faq`](ayunit://docs/asset/faq) | **First, for any classification doubt.** Explains the two independent hierarchies (operational `AssetGroup→SecurityType` vs strategy/report `Product→AssetClass`), Offshore, TaxRegime/gross-up, soft-delete — in PT. The single most common mistake (`Renda Fixa` is a **Product**, not an `AssetGroup`) is called out here. |
| [`ayunit://docs/asset/relationship`](ayunit://docs/asset/relationship) | The PK/FK hub model — the 10 outbound FKs (which classification strings must resolve) and 4 that are nullable (`Benchmark`, `Seniority`, `Source`/PriceSource, `Issuer`); the inbound-FK list; what `v_AssetCustody` adds. |
| [`ayunit://docs/asset/procedure`](ayunit://docs/asset/procedure) | The full `Global.Asset_Update` param catalog: the 10 NOT-NULL required params for `I`, the optional groups (identifiers / fixed-income / fund-liquidity / pricing), and the 5 logic-only params that are **not** stored columns. |
| `get_view_detail('Global.v_Asset')` / `get_table_detail('Global.Asset')` | Exact columns/types live. Schema is **not** mirrored in docs — introspect when you need a type or to confirm a column exists. |

## Tools you call directly

- `execute_select_query` — every read: peer lookup, duplicate check, lookup-value validation, verify.
- `execute_procedure(procedure='Global.Asset_Update', cmd='I', …)` — the only write path.
- `get_view_detail` / `get_table_detail` — confirm columns/types; never guess.

> Off-MCP, the same calls go through the Ayunit REST API: `POST /api/v1/introspection/AgnesOrg00DB/query`
> for reads and `…/execute-procedure` for the write (creds in [`.env`](../../../.env)). Identical contract.
> **This is production — never insert without explicit user confirmation of the previewed row.**

## The registration cycle

### 1 — Normalise & duplicate-check (gate: must be empty)

Normalise identifiers first: strip CNPJ punctuation (`04.839.017/0001-98` → `04839017000198`),
uppercase ISIN/CUSIP/Bloomberg. Then make sure the instrument isn't **already** registered — under
the proposed code *or* under a different code carrying one of its identifiers (a silent duplicate is
worse than a rejected insert):

```sql
SELECT pk_AssetID, Asset, Description, Isin, Cnpj, AnbimaCode, Activated
FROM Global.v_Asset
WHERE Asset = '<proposed_code>'
   OR Isin = '<isin>' OR Cnpj = '<cnpj>' OR AnbimaCode = '<anbima>'
   OR FundCode = '<fundcode>' OR BbgCode = '<bbg>' OR ExchangeCode = '<exch>';
```

- Match on `Asset` → **refuse**; the code is UNIQUE. (If it's `Activated = 0`, the right move is
  usually `U` to reactivate, not a new row — hand off to the `ayunit_asset` prompt.)
- Match on an identifier under a *different* `Asset` → **stop and surface it**; likely the same
  instrument already booked. Ask the user before creating a parallel row.
- Empty → proceed.

### 2 — Classify by peer analogy (the core of "respect the logic")

Never invent a classification. Pull assets of the **same kind already in the book** and copy their
convention. Search by the strongest signal you have, in this order:

1. **Same issuer / same family** (most precise) — other tranches of the issuer, other share classes
   of the fund manager:
   ```sql
   SELECT TOP 20 Asset, Description, AssetGroup, SecurityType, Product, AssetClass,
          Benchmark, Source, TaxRegime, CouponType, FloatAsset, Issuer
   FROM Global.v_Asset
   WHERE Issuer LIKE '%<issuer>%' OR Description LIKE '%<issuer>%'
   ORDER BY pk_AssetID DESC;
   ```
2. **Same instrument type** — if no issuer match, anchor on the security type (`CDB`, `Debênture`,
   `NTN B`, `LCI`, `CRI`, `Fundos`, `Stock`, …) and read the dominant combo:
   ```sql
   SELECT AssetGroup, SecurityType, Product, AssetClass, Benchmark, Source,
          TaxRegime, COUNT(*) AS n
   FROM Global.v_Asset
   WHERE SecurityType = '<type>'        -- or AssetGroup = '<group>'
   GROUP BY AssetGroup, SecurityType, Product, AssetClass, Benchmark, Source, TaxRegime
   ORDER BY n DESC;
   ```

Adopt the **dominant** peer combo. The cheat-sheet below is the expected answer for the common types
(grounded in live data on 2026-05-25) — but the **live DB is source of truth**; if peers disagree
with the sheet, follow the peers and tell the user.

| New instrument | AssetGroup | SecurityType | Product | AssetClass (typical) | Benchmark | Source | TaxRegime |
|---|---|---|---|---|---|---|---|
| Debênture incentivada (infra, IPCA) | `Debênture` | `Debênture` | `Renda Fixa` | `Debênture Incentivada` / `CP Juros Real` | `IPCA` | `Anbima` | `Isento` |
| Debênture comum | `Debênture` | `Debênture` | `Renda Fixa` | `CP Pós Fixado` / `CP Juros Real` | `CDI`/`IPCA` | `Anbima` | `Regressivo` |
| CDB | `RF Não Listada` | `CDB` | `Renda Fixa` | `CP Pós Fixado` | `CDI` | `BTG` (custody) | `Regressivo` |
| LCI / LCA | `LCI/LCA` | `LCI` / `LCA` | `Renda Fixa` | `CP Pós Fixado` | `CDI` | `BTG` | `Isento` |
| CRI / CRA | `CRI/CRA` | `CRI` / `CRA` | `Renda Fixa` | `CP Pós Fixado` / `Imobiliário` | `CDI`/`IPCA` | `Anbima` | `Isento` |
| Tesouro IPCA (NTN-B) | `Tesouro` | `NTN B` | `Renda Fixa` | `Juros Real Soberano` | `IPCA` | `Anbima` | `Regressivo` |
| Tesouro Selic / Pré (LFT / LTN) | `Tesouro` | `LFT` / `LTN` | `Renda Fixa` | `Pós Fixado Soberano` / `Pré Fixado Soberano` | — | `Anbima` | `Regressivo` |
| Ação BR | `Equity` | `Stock` | `Renda Variável` | *(setor: `Financials`, `Industrials`, …)* | `IBOV` | `Exchange` | — |
| Fundo (FIM/FIC) | `Fundos` | `Fundos` | `Multimercado`/`Renda Fixa`/`Alternativos`/`Previdência` | *matches Product* | `CDI` (se aplicável) | (custody/`BTG`) | — |
| FII | `FII` | `Fundos` | `Renda Variável`/`Renda Fixa` | `FII` / `Imobiliário` | `IFIX` | `Exchange` | — |

> ⚠️ **The two hierarchies are independent.** `AssetGroup→SecurityType` is operational (drives the
> pricing routine; not in reports); `Product→AssetClass` is strategy/reporting (PnL-by-strategy is at
> `AssetClass`). Don't cross them — `Renda Fixa` is a `Product`, never an `AssetGroup`. Full valid
> combos: re-run the GROUP BY in step 2 (41 `AssetGroup→SecurityType`, 89 `Product→AssetClass` live).

### 3 — Fill the type-specific optional fields (from the same peers)

Copy the field *shape* the peers use, with this asset's actual values:

- **Fixed income** (debênture, CDB, LCI, CRI, treasury): `Maturity`; `Issuer`; `FloatAsset` = the
  index (`CDI`/`IPCA`); `CouponType` (`Spread` for IPCA+spread debs/CRIs, `Times` for `%CDI` CDBs,
  `Fixed` for NTN-B); `CouponFrequency` (e.g. `2` = semiannual); `Spread` **as a decimal fraction**
  (`4.8791%` → `0.048791`); `FixedRate`/`FloatRate` likewise; `TaxRate` where peers set it.
- **Identifiers**: fill every standardised code you have — `Isin`, `AnbimaCode`, `Cnpj`, `FundCode`,
  `ClassCode`, `BbgCode`, `ExchangeCode`. For BR fixed income the `Asset` code is frequently the
  `AnbimaCode` itself (matches peers like `CCROA5`, `23H1191899`).
- **Fund liquidity** (Fundos/FII): `SubscriptionDaysToQuote`, `RedemptionDaysToQuote`,
  `RedemptionDaysToSettle` (D+x).
- **Always**: `ContractSize = 1` unless peers say otherwise (derivatives/bonds differ);
  `Offshore = 1` only for non-BRL/offshore assets (`Offshore = 1` forces `PriceExFee = Price`);
  `Activated = 1`.

### 4 — Validate every classification string against its lookup table

`Global.Asset_Update` resolves each classification **string** to its `fk_*` id via the lookup table.
A value that doesn't already exist there will fail the insert (or NULL a NOT-NULL FK). So confirm
each one exists **before** writing — these are the values live as of 2026-05-25:

- `Currency` ∈ `AUD, BRL, CAD, CHF, CNH, CNY, EUR, GBP, HKD, RUB, USD`
- `Source` (PriceSource) ∈ `Anbima, BBG, BTG, Exchange, MS, Santander, XP`
- `TaxRegime` ∈ `Isento, Longo Prazo, Previdência, Regressivo` (or NULL)
- `AssetGroup` / `SecurityType` / `Product` / `AssetClass` / `Benchmark` — validate against the
  distinct values returned by step 2's GROUP BY (don't hardcode; they grow).

```sql
SELECT DISTINCT AssetClass FROM Global.v_Asset ORDER BY AssetClass;  -- repeat per column in doubt
```

If a needed value is **genuinely new** (a classification the firm has never used), **stop** — do not
introduce a new `AssetGroup`/`AssetClass`/`Benchmark`/etc. on your own. New lookup values are a
deliberate taxonomy decision; surface it to the user and let them decide.

### 5 — Preview & confirm (mandatory, every time)

Show the user the **complete row you're about to insert** as a table — every param with its value,
the 10 required ones first, then the optionals you filled — plus a one-line note on **which peers you
copied** (e.g. *"classificação copiada de CCROA5 / CEED13, debêntures incentivadas IPCA"*). Translate
`Offshore` (`0/1` → não/sim). Wait for an explicit yes. Never insert on implied approval.

### 6 — Insert

```
execute_procedure(
  database  = "AgnesOrg00DB",
  procedure = "Global.Asset_Update",
  cmd       = "I",
  params    = {
    "Asset":"<code>", "Description":"<name>", "Currency":"BRL", "Offshore":0,
    "AssetGroup":"Debênture", "SecurityType":"Debênture",
    "Product":"Renda Fixa", "AssetClass":"Debênture Incentivada",
    "ContractSize":1, "Activated":1,
    /* optionals copied from peers: */
    "Maturity":"2033-11-15", "Issuer":"<issuer>", "AnbimaCode":"<code>", "Isin":"<isin>",
    "Benchmark":"IPCA", "Source":"Anbima", "TaxRegime":"Isento",
    "CouponType":"Spread", "CouponFrequency":2, "FloatAsset":"IPCA", "Spread":0.048791
  }
)
```

The proc populates the audit columns (`InputDate`, `fk_InputUserID`) itself — there is no param for
them. The 5 logic-only params (`@MaxMaturity`, `@identifierInput`, `@Client`, `@Account`,
`@Identifier`) are **not** stored columns — leave them unset for a registration.

### 7 — Verify

Re-SELECT the row you just created and show it back, confirming the new `pk_AssetID` and that every
FK resolved (no NULL where you passed a value):

```sql
SELECT pk_AssetID, Asset, Description, AssetGroup, SecurityType, Product, AssetClass,
       Currency, Offshore, Benchmark, Source, Issuer, TaxRegime, Maturity, Activated
FROM Global.v_Asset WHERE Asset = '<code>';
```

If a classification came back NULL, the lookup string didn't match — the value is wrong (or
not in the lookup). Fix and re-run; don't leave a half-classified asset.

### 8 — Downstream (note, don't do unless asked)

A bare `Global.Asset` row is invisible to a custody feed until `Portfolio.AssetCustody` maps the
custodian's ticker/scaling to it (`v_AssetCustody`: `TickerCustody`, `PositionFactor`,
`PriceFactor`). If the asset will arrive on a BTG/XP/… feed, tell the user that mapping is the next
step — it's a **separate** procedure outside this skill's scope. Likewise prices flow via
`fk_PriceSourceID` + `Global.PriceSourcePreference` (see the prices domain).

## Critical rules

- **Duplicate gate first** — never insert if the `Asset` code exists, or an identifier already maps
  to another asset. UNIQUE on `Asset`; a silent identifier-duplicate is worse than a hard failure.
- **Classify by peers, never by guessing** — every classification value must come from (and match)
  an existing peer's convention and exist in its lookup table. When peers and the cheat-sheet
  disagree, the live peers win.
- **Never invent a new lookup value** (`AssetGroup`/`SecurityType`/`Product`/`AssetClass`/`Benchmark`/
  `Source`/`Issuer`) — that's a taxonomy decision for the user.
- **Don't cross the two hierarchies** — operational (`AssetGroup→SecurityType`) ≠ strategy
  (`Product→AssetClass`). `Renda Fixa` is a Product, not a group.
- **All 10 NOT-NULL params required** for `I`: `Asset, Description, Currency, Offshore, AssetGroup,
  SecurityType, Product, AssetClass, ContractSize, Activated`. Ask for any missing — never default
  a classification.
- **Spread/rates are decimal fractions** (`4.8791%` → `0.048791`); **dates** `YYYY-MM-DD`;
  `Offshore`/`Activated` as `0/1`.
- **Preview-and-confirm before the write; verify after.** No insert on implied approval.
- **Reply in the user's language** and echo the resolved scope (identifier + normalised form).

## When unsure

- **No peer of this kind exists** (a brand-new instrument family) → present the closest peers you
  found, propose a classification, and ask the user to confirm each of the 4 classification fields
  before inserting. Don't silently pick.
- **Identifier matches an existing asset under another code** → it's probably already booked. Show
  both and ask whether to reactivate/update the existing row instead of creating a new one.
- **A classification string verifies back as NULL** → it isn't in the lookup table. Re-check spelling
  against `SELECT DISTINCT <col> FROM Global.v_Asset`; if truly new, escalate (don't force it).
- **User wants a bulk load** (e.g. a whole new debenture series) → same logic, but do one bulk
  duplicate pre-flight, preview the batch (N total / N duplicates / N net), get one approval, then
  loop `execute_procedure`. Canary the first row and verify before the rest.
- **User asks to update/soft-delete instead of register** → that's the `ayunit_asset` prompt
  (SELECT-first-merge for `U`; `Activated = 0` for soft-delete given ~19 inbound FKs). Hand off.
