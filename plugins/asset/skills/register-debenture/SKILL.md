---
name: register-debenture
description: Use when the user wants to register / cadastrar a Brazilian DEBENTURE in Global.Asset starting from its Anbima code or ISIN ("cadastra a debênture TAEEB2", "registra essa debênture", "puxa os dados da Anbima da debênture"), or to complete/reconcile an already-registered debenture against the Anbima register (missing Issuer, Maturity, Spread, ISIN, …). Fetches the Anbima cadastral snapshot via get_debenture_register_data, parses the remuneration expression, maps it to the Global.Asset layout, then either hands off to asset-register (new) or proposes a gap-fill U on the existing row. BR debentures only — not CDB/CRI/CRA/funds/offshore bonds.
---

# Register / complete a debenture in `Global.Asset` from the Anbima register

You are the specialist for driving a **Brazilian debenture** to a coherent state in
`Global.Asset`, using the Anbima register as the data source. Two branches, decided by a
mandatory resolve step:

- **Not in the book** → build a pre-filled payload and **hand off to `asset-register`**
  (which owns the duplicate gate, peer-analogy classification, preview, INSERT, verify).
  This skill never INSERTs on its own — same contract as `register-br-funds`.
- **Already in the book** (the common case for codes arriving on feeds) → field-by-field
  comparison against the Anbima data, then a **gap-fill `U`** that completes *missing*
  columns only (e.g. a NULL `Issuer`), with preview + confirm + verify.

## Inputs

One or more debenture identifiers: Anbima `codigo_ativo` (e.g. `TAEEB2`, `CMTR13`) and/or
ISIN (`BRTAEEDBS0P6`), mixed freely. Do **not** pass CNPJs, Bloomberg codes or Ayunit
`Asset` codes to the register tool — it matches only codigo_ativo / ISIN. Echo back the
resolved scope (code, ISIN, existing `pk_AssetID` if any) at the top of every reply.
**Reply in the user's language (PT/EN).**

## Reference resources (read on demand)

| Resource | Read when… |
|---|---|
| [`ayunit://docs/asset/faq`](ayunit://docs/asset/faq) | Any classification doubt — the two independent hierarchies (`AssetGroup→SecurityType` operational vs `Product→AssetClass` strategy), `TaxRegime`/gross-up, `Activated` semantics. |
| [`ayunit://docs/asset/procedure`](ayunit://docs/asset/procedure) | `Global.Asset_Update` param catalog and its traps — silent FK resolution (a typo in a nullable FK like `Issuer` **writes NULL with no error**), `U` overwrites every omitted column. |
| [`ayunit://docs/asset/relationship`](ayunit://docs/asset/relationship) | Which classification FKs are NOT NULL vs nullable (`Benchmark`, `Seniority`, `Source`, `Issuer`). |

## Tools you call directly

- `get_debenture_register_data(identifiers=[…])` — the Anbima register fetch (read-only).
- `identify_asset` / `execute_select_query` — resolve step, issuer-lookup match, verify.
- `execute_procedure(procedure='Global.Asset_Update', cmd='U', …)` — the gap-fill write
  (exists-branch only; the INSERT path belongs to `asset-register`).

## The cycle

### 1 — Resolve first (decides the branch)

Per the tool's own contract: *pair it with `identify_asset` first — if the code already
resolves to a `Global.Asset` row there may be nothing to register.* Check both identifiers:

```sql
SELECT pk_AssetID, Asset, Description, AssetGroup, SecurityType, Product, AssetClass,
       Currency, Offshore, Benchmark, Source, Issuer, TaxRegime, Maturity,
       CouponType, CouponFrequency, FloatAsset, Spread, FixedRate, FloatRate,
       Isin, AnbimaCode, ContractSize, Activated
FROM Global.v_Asset
WHERE Asset = '<code>' OR AnbimaCode = '<code>' OR Isin = '<isin>';
```

Hit → **exists branch** (step 4B). Empty → **new branch** (step 4A). Either way, step 2
runs next — the Anbima data is needed in both.

### 2 — Fetch the Anbima register data

```
get_debenture_register_data(identifiers=["TAEEB2"])          # code and/or ISIN, mixed
```

Returns one row per debenture (passing both identifiers of the same debenture yields ONE
row). **Miss-mode caveat:** the backend is a snapshot of ONE Anbima reference date
(`data_referencia`, typically the last business day) — a debenture with **no Anbima quote
that day is simply absent** (`identifiers_not_found`). Report that as "no Anbima data on
the reference date", NOT as "the debenture doesn't exist"; ask the user for the missing
attributes or retry another day.

### 3 — Map Anbima → `Global.Asset` layout

The register columns that map to stored columns:

| Anbima field | → Global.Asset | Notes |
|---|---|---|
| `codigo_ativo` | `Asset` + `AnbimaCode` | House convention: for BR fixed income the `Asset` code IS the Anbima code (peers: `TAEEB2`, `CCROA5`). |
| `ISIN` | `Isin` | Uppercase. |
| `data_vencimento` | `Maturity` | `YYYY-MM-DD`. |
| `emissor` | `Issuer` | **Only after resolving against the `Global.Issuer` lookup** — see below. Never pass the raw Anbima string. |
| `percentual_taxa` (+ `grupo`) | `FloatAsset`, `Benchmark`, `CouponType`, `Spread` / `FloatRate` / `FixedRate` | Parse per the table below. |
| `Lei 12.431` | `TaxRegime` hint | `Sim` → incentivada → `TaxRegime = 'Isento'` and AssetClass hint `Debênture Incentivada`; `Não` → typically `Regressivo`. A **hint** to confirm against peers — per `asset/faq`, the regime is per-asset, not mechanical. |
| — (fixed) | `Currency = 'BRL'`, `Offshore = 0`, `ContractSize = 1`, `Activated = 1`, `Source = 'Anbima'` | The standard BR-debenture shape; confirm `Source` against peers. |

**Parsing `percentual_taxa`** (BR comma decimals — `5,6000%` → `0.056`, always decimal
fractions in the DB):

| Expression form | Example | CouponType | Fields set |
|---|---|---|---|
| `IPCA + x%` | `IPCA + 5,6000%` | `Spread` | `FloatAsset='IPCA'`, `Benchmark='IPCA'`, `Spread=0.056` |
| `DI + x%` | `DI + 1,9500%` | `Spread` | `FloatAsset='CDI'`, `Benchmark='CDI'`, `Spread=0.0195` — the book says **CDI**, Anbima says DI |
| `x% do DI` | `105,00% do DI` | `Times` | `FloatAsset='CDI'`, `Benchmark='CDI'`, `FloatRate=1.05` |
| flat `x%` (prefixada) | `12,5000%` | `Fixed` | `FixedRate=0.125`, no FloatAsset |

Cross-check the parse against `grupo` (`IPCA SPREAD`, `DI SPREAD`, `DI PERCENTUAL`, …) —
if they disagree, stop and show both to the user.

**Description** follows the house pattern observed in peers:
`DEB <ISSUER SHORT NAME> <INDEX> + <rate>% <DD-MON-YYYY>` (e.g.
`DEB TRANS. ALIANÇA IPCA + 5.60% 15-APR-2029`). Max 100 chars.

**Issuer lookup match** — `fk_IssuerID` is a nullable FK and `Global.Asset_Update`
resolves it **silently**: an unmatched string writes NULL with no error (per
`asset/procedure`). So resolve first:

```sql
SELECT pk_IssuerID, Issuer FROM Global.Issuer
WHERE Issuer LIKE '%<distinctive word of emissor>%';   -- e.g. '%ALIANÇA%', '%TAESA%'
```

Exactly one plausible match → use that exact `Issuer` string. Zero or ambiguous → leave
`Issuer` out and surface it: creating a new `Global.Issuer` row is a deliberate decision
for the user, never yours (no-new-lookup-values rule).

**NOT mapped** (pricing/analytics snapshot, not register data — never write these):
`pu`, `taxa_indicativa`, `taxa_compra`, `taxa_venda`, `VNA`, `duration`, `percent_pu_par`,
`desvio_padrao`, `percent_reune`, `referencia_ntnb`. Prices flow through the prices
domain (`asset-price-history`), not through `Global.Asset`.

**Report-only** (no Global.Asset column; show them in the summary — they inform
`Description` and the user's judgement): `Emissão`, `Série`, `Data da emissão`,
`Resgate antecipado`, `Artigo`.

**Not derivable from the feed:** `CouponFrequency` (typically `2` = semiannual for BR
debentures) and `AssetClass` — both come from peers/user in the next step, never from
this tool.

### 4A — New branch: hand off to `asset-register`

Present the mapped payload (mapped fields + report-only fields + what's missing), then
**invoke the `asset-register` skill** with it. That skill owns the rest of the cycle:
duplicate + identifier gate, peer-analogy classification (which settles `AssetClass`,
`TaxRegime`, `Source`, `CouponFrequency` against live debenture peers), lookup
validation, preview-and-confirm, INSERT, verify. Do not shortcut it — the Anbima data is
*input* to registration, not a substitute for the peer check.

### 4B — Exists branch: audit + gap-fill

1. **Compare** — show a field-by-field table: column | DB value | Anbima value | verdict
   (`ok` match / `gap` DB NULL, Anbima has it / `mismatch` both set, different).
2. **Gaps** → propose filling them. Typical real case: `Issuer` NULL while Anbima carries
   the emissor (fill only if the lookup resolves, per step 3). Nothing to fill and no
   mismatches → say the row is complete and coherent, and stop.
3. **Mismatches** (e.g. DB `Maturity` ≠ Anbima `data_vencimento`) → **report, never
   overwrite.** Populated DB values may be deliberate; changing one is the user's call.
   Only include a mismatched field in the `U` if the user explicitly says so.
4. **The `U` is SELECT-first-merge** — per `asset/procedure`, `U` overwrites EVERY
   column: omitted params become NULL. Carry **every populated field** from the step 1
   SELECT into the params, then apply only the gap-fills:

```
execute_procedure(
  database  = "AgnesOrg00DB",
  procedure = "Global.Asset_Update",
  cmd       = "U",
  params    = {
    "pk_AssetID": 1436,
    /* …every populated field from the SELECT, unchanged… */
    "Issuer": "<exact Global.Issuer string>"   /* the only new value */
  }
)
```

5. **Preview + confirm (mandatory)** — show the full param set, highlight what changes
   vs what's carried, wait for an explicit yes. Never write on implied approval.
6. **Verify** — re-SELECT the row; every carried field unchanged, every filled field
   resolved (not NULL). A filled classification coming back NULL means the lookup string
   didn't match — fix and re-run, don't leave it half-done.

## Critical rules

- **Resolve before fetch-and-map decides everything** — never register a code that
  already resolves (that's a duplicate), never `U` a row that doesn't exist.
- **This skill never INSERTs.** New debentures go through `asset-register` — one insert
  path in the plugin, with the peer-analogy and duplicate gates intact.
- **Gap-fill fills NULLs only.** Mismatches on populated columns are reported, never
  silently overwritten.
- **`U` = SELECT-first-merge, always** — omitted params become NULL on the row.
- **Issuer (and every nullable FK) must resolve against its lookup before the call** —
  the procedure writes NULL silently on a miss. Never invent a new lookup value.
- **Rates are decimal fractions** (`5,6000%` → `0.056` — mind the BR comma), dates
  `YYYY-MM-DD`, `DI` in the feed is `CDI` in the book.
- **Never write Anbima pricing fields** (`pu`, `VNA`, `taxa_*`, `duration`, …) into
  `Global.Asset`.
- **Preview-and-confirm before every write; verify after. Reply in the user's language.**

## When unsure

- **Tool returns the code in `identifiers_not_found`** → no Anbima quote on the
  reference date, not proof of non-existence. Say so; register can still proceed via
  `asset-register` with user-provided attributes.
- **`grupo` and the parsed `percentual_taxa` disagree**, or the expression matches no
  known form (e.g. hybrid/stepped rates) → show the raw string and the attempted parse;
  let the user settle the coupon fields.
- **Issuer lookup gives zero or multiple candidates** → list candidates (or the miss),
  leave `Issuer` unset, and ask — a new issuer row is a user decision.
- **Multiple codes at once** (a series, a portfolio's debentures) → one
  `get_debenture_register_data` call with all identifiers, then loop the cycle per
  debenture; batch the exists-branch comparison into one table per debenture and get
  one approval per write.
- **The instrument turns out not to be a debenture** (CRI/CRA/CDB carried in a debenture
  list) → this tool won't have it; route to `asset-register` (or `register-br-funds` for
  funds) directly.
