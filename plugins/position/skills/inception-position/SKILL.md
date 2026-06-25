---
name: inception-position
description: Use when the user wants to seed / cadastrar the FIRST position of a newly-onboarded account in Portfolio.AccountPosition (the Inception Position) — one snapshot row per held asset on the cutoff date. The pipeline invariant is position[D] = transform(position[D-1] + trades[D]), so day 0 has no D-1 to roll from and must be seeded by hand. This skill runs an exhaustive pre-flight (refuses to proceed if ANY position or CheckedDate already exists for the pair), builds the inception rows with Open=Close and zero flows, canaries one, batches the rest via execute_procedure (Portfolio.AccountPosition_Update I), verifies, and reconciles against custody. It does NOT create the CheckedDate lock — after the positions are inserted and validated, it tells the user the seed is in and that they must add the CheckedDate themselves to freeze it. The onboarding seed, not a daily write.
---

# Seed an Inception Position for a newly-onboarded account

You are the orchestrator for **inception**: writing the very first `Portfolio.AccountPosition`
rows for a `(Account, Custody)` pair that has none yet. Once those rows are in and validated,
the seed still has to be **frozen** by a `Portfolio.CheckedDate` so the pipeline picks up from
`D+1` — but **this skill does not write that lock.** It seeds and reconciles the positions, then
hands the lock back to the user as an explicit manual step (see §8). The lock is audit-sensitive
and is intentionally left as a deliberate human action, separate from the position write.

This is the legitimate exception to "never write `AccountPosition` by hand" — outside inception,
fix the upstream **transaction** and let the pipeline recompute. The full recipe (column-by-column
defaults, gotchas, rationale) is **[`ayunit://docs/position/inception`](ayunit://docs/position/inception)** —
**read it first**; this skill is the *batch* procedure (exhaustive pre-flight → draft → preview →
canary → batch → verify → reconcile → **advise the user to add the CheckedDate**) wrapped around it.

## Allowlist & write model

Both procedures are in the MCP allowlist:

| Procedure | Allowlisted CMDs | This skill uses |
|---|---|---|
| `Portfolio.AccountPosition_Update` | `I`, `U`, `D` | `I` for each held asset (inception); `D` for mid-batch rollback or dedup; `U` not used by inception |
| `Portfolio.CheckedDate_Update` | `I`, `U` | **none — this skill does not write the lock.** It only *reads* `v_CheckedDate` in pre-flight (§1d) and verify (§9). Creating the lock is a manual step the user performs after the seed is validated (§8). |

**`S` (select) is intentionally NOT allowlisted** for these procs — all reads in this skill
(pre-flight, holdings lookup, canary/batch verify, reconciliation) go through
**`execute_select_query`** on `v_AccountPosition` / `v_CheckedDate` / `v_CustodyPosition`, which
is simpler and has identical behaviour. Don't reach for `@CMD='S'`; if you call it you'll get
`CMD 'S' not allowed … Allowed: ['I','U','D']`.

Calls go through **`execute_procedure`** — there is no `BEGIN TRAN` wrapping multiple calls
(each `execute_procedure` is its own server-side transaction). That means a mid-batch failure
leaves a **partial seed**; the canary + per-row outcome tracking + rollback step (§9) exist
specifically to handle that.

> Off-MCP, the same calls go through the Ayunit REST API `POST /api/v1/introspection/{db}/execute-procedure`
> (creds in [`.env`](../../../.env)). For SSMS-only contexts (no MCP/REST access at all), an
> alternative copy-paste `EXEC` template still lives in the inception doc — but the in-MCP path
> below is the default. **This is production — never call the writes without the user confirming
> the previewed batch.**

## What the procedure actually persists on `@CMD='I'` (verified against the live contract)

`get_procedure_detail('Portfolio.AccountPosition_Update')` confirms **42 params**, but the
`I` branch only writes a subset of them. Knowing this keeps the canary-verify (§6) honest —
otherwise you'll flag "mismatches" that are really the proc doing its job.

- **`@AccountCurrency` and `@AccountFx` are recomputed server-side — they are NOT stored as
  sent.** The proc header literally says *"Aceito; recomputado internamente"*. On INSERT it
  resolves `AccountCurrency` from the `(Account, Custody)`'s `Global.ClientAccount.Currency`,
  and computes `AccountFx = AssetCurrency_USD / AccountCurrency_USD` from `AssetData.v_Price`
  on `Date` (all prices are stored in USD; same-currency or USD → `1.0`). So passing
  `AccountFx`/`AccountCurrency` is harmless but a no-op — **don't expect the persisted row to
  echo what you sent for these two.** The §3 cross-currency note below reflects this.
- **`Product`, `AssetClass`, `AssetGroup`, `Client` are NOT in the INSERT column list.** They're
  informational params used only by the `S`/`PI` branches; the view re-derives them by joining
  out `fk_AssetID` / `fk_ClientAccountID`. Passing them is harmless (and fine for completeness),
  but they don't persist from the params — so a verify that reads them back is checking the
  Asset/Account registration, not your inception call.
- **After-tax & PnlDaily have COALESCE fallbacks on INSERT:** `ValueOpenAfterTaxes ← ValueOpen`,
  `ValueCloseAfterTaxes ← ValueClose`, `PnlDaily ← PnlExGrossUp` when omitted. Matches the
  "set after-tax = gross when unknown" default — you may omit them and the proc fills gross.
- **The lock guard is `Date >= @Date AND Activated <> 0`:** the `I` branch refuses the insert
  if an active `CheckedDate` exists at `Date >= the position's Date` for the pair. This is why
  pre-flight §1d refuses to proceed if any `CheckedDate` already exists — the seed must go in
  *before* any lock. This skill never creates the lock itself (the user does, §8), so on the
  happy path no lock exists while the positions are being written.
- **`@CMD='PI'` ("Posição Manual") is a built-in server-side preview** that resolves the asset
  by *any* identifier (`Asset`/`Bbg`/`Isin`/`Cusip`/`AnbimaCode`/`FundCode`/`ClassCode`/`Cnpj`/
  `ExchangeCode`), pulls the last price `<= Date`, and returns a fully-computed inception-shaped
  row (`ValueClose = Qty × Price × ContractSize`, `Open = Close`, flows = 0, `cmd='I'`) **without
  inserting**. It would be the ideal driver for §5's preview — but **`PI` is not on the MCP
  allowlist** (only `I/U/D` are), so `execute_procedure` can't call it today and
  `execute_select_query` rejects `EXEC`. Until `PI` is allowlisted, build the preview by hand
  (§3–§5). If/when it's added, prefer it — it computes `ValueClose` with the asset's real
  `ContractSize`, which is exactly the §4 sanity gate.

## Inputs

A **`Account`** (`ClientAccount` string), its **`Custody`**, and the **inception date** (the
cutoff day, after which the pipeline takes over). Plus the **holdings on that date** — typically
sourced from a custody extract already ingested into `Portfolio.CustodyPosition` (via
[`ayunit://docs/position/ingestion`](ayunit://docs/position/ingestion)), or supplied directly by
the user. Echo the resolved `(Account, Custody, Date)` triple at the top of every reply.

## Reference resources (read on demand)

| Resource | Read when… |
|---|---|
| [`ayunit://docs/position/inception`](ayunit://docs/position/inception) | **First.** The full inception recipe — pre-flight gates, the column-by-column defaults table, the EXEC template, gotchas (cross-currency, cash/FX, lending/margin subsets, bond accrued interest, cost-basis import, empty inception), and rollback. |
| [`ayunit://docs/position/procedures`](ayunit://docs/position/procedures) | The `AccountPosition_Update` param catalog (42 params) — confirms "why writes are rare" + the param-by-param contract. |
| [`ayunit://docs/checkeddate/usage`](ayunit://docs/checkeddate/usage) | The lock contract: a `CheckedDate` at `Date = D` rejects every write to `AccountTransaction` / `AccountPosition` with `Date ≤ D` for that `(Account, Custody)`. Explains why positions must be written **before** the lock. |
| [`ayunit://docs/position/faq`](ayunit://docs/position/faq) | Column meaning when in doubt — `QuantityOpen` vs `Close` vs `Transaction`, `AvgPrice` (rolling weighted-avg cost since `AcquisitionDate`), P&L decomposition (`PnlExGrossUp + PnlGrossUp − SellIncomeTaxes`), `Itd` window, lending/margin as **subsets** of `QuantityClose`. |
| [`ayunit://docs/position/ingestion`](ayunit://docs/position/ingestion) | When the holdings come from a custody file — parse/map/validate the extract into `CustodyPosition` first, then lift the resolved rows for the inception. |
| [`ayunit://docs/asset/procedure`](ayunit://docs/asset/procedure) | When a held asset isn't in `Global.Asset` yet — register it via the `asset-register` skill **before** drafting the inception (the proc resolves `@Asset` → `fk_AssetID`; unknown code → row absent from `v_AccountPosition`). |
| `get_view_detail('Portfolio.v_AccountPosition')` · `get_procedure_detail('Portfolio.AccountPosition_Update')` | Live column/param contract — schema isn't mirrored in docs. |
| [`ayunit://docs/position/reconciliation`](ayunit://docs/position/reconciliation) | The `AssetR → Asset` bridge model — what `Portfolio.v_AssetCustody` implements, how `Update_Missing_Asset` back-fills `fk_AssetID` on resolved rows. Consult when an `AssetR` is ambiguous (multiple `Asset` candidates), or for the `IsinR` / `AnbimaCodeR` / `CetipCode` fallback order §2a's resolution loop uses. |
| [`references/assetcustody-resolution.md`](references/assetcustody-resolution.md) | **§2a only.** The full untranslated-`AssetR` resolution sub-flow — detect → resolve → preview `AssetCustody_Update I` → confirm → insert → `Update_Missing_Asset` back-fill → verify. Read when §1e-B counts `n_unresolved > 0`; otherwise skip. |

## Tools you call directly

- `execute_select_query` — every pre-flight check, holding lookup, lookup-value validation, verify, mid-batch rollback lookup.
- `execute_procedure(procedure='Portfolio.AssetCustody_Update', cmd='I', …)` — inside §2a, once per untranslated `AssetR` after the user confirms the proposed `(TickerCustody, Custody) → Asset` mapping.
- `execute_procedure(procedure='Portfolio.CustodyPosition_Update', cmd='Update_Missing_Asset', …)` — inside §2a, once at the end, **scoped narrowly** to `@Date=<inception_date>`, `@Account=<account>`, `@Custody=<custody>`. Back-fills `fk_AssetID` on previously-untranslated rows; this is a WRITE despite the name.
- `execute_procedure(procedure='Portfolio.AccountPosition_Update', cmd='I', …)` — per held asset, in the canary + batch.
- `execute_procedure(procedure='Portfolio.AccountPosition_Update', cmd='D', …)` — **only** for mid-batch rollback (§9).
- `get_view_detail` / `get_procedure_detail` — confirm columns/params; never guess.

## The inception cycle

### The default contract: preview → confirm → execute → hand off the lock

Every inception run follows the same shape, and the first three beats are **not** optional:

1. **Preview** — after pre-flight and the transform, show the user the *complete* inception
   batch that's about to be written (§5): every row, every populated column, as a table, with
   the resolved `(Account, Custody, Date)` and asset count. Plainly state what will happen — *N*
   `AccountPosition` inserts (canary first). Make clear the skill will **not** create the
   CheckedDate; that's a manual step the user does afterward.
2. **Confirm** — ask "is everything correct? confirm to insert?" and **wait for an explicit
   yes**. No write happens on silence, a vague reply, or implied approval. If the user wants a
   change, revise the draft and re-preview — don't write a partially-agreed batch.
3. **Execute** — only on explicit confirmation, run the positions: canary the largest row and
   pause once more (§6), then batch the rest (§7), then verify and reconcile (§9). The §6 canary
   pause is a *second* checkpoint inside execution, not a replacement for the §5 confirm.
4. **Hand off the lock** — once the positions are validated and reconciled, **tell the user the
   seed is in and that they must add the `CheckedDate` themselves** to freeze it (§8). The skill
   does not write the lock.

This is the spine of the skill — §5 through §9 are the detailed mechanics. Never collapse the
first three beats: never execute without the §5 preview, never skip the explicit yes. And never
attempt to write the `CheckedDate` — only advise the user to create it.

### 1 — Exhaustive pre-flight (the primary gate — refuse to proceed if ANY hit)

This is the most important step in the skill. The user's onboarding flow depends on this seed
being **the** first record for the `(Account, Custody)` pair; if there is *any* prior
`AccountPosition` row or *any* `CheckedDate` for them — active or not — this is **not** an
inception and you must stop. Don't trust the view alone — the view INNER-JOINs out
`fk_AssetID`/`fk_CustodyID`/`fk_CurrencyID`, so a base-table row with any NULL FK is invisible.
Check both.

Refuse to proceed until **every one** of these returns the "clean" answer.

**1a. `ClientAccount` is registered and matches the requested `Custody`:**

```sql
SELECT ClientAccount, Nickname, Client, Custody, Currency, ApplyGrossUp, Offshore, InputDate
FROM Global.v_ClientAccount
WHERE ClientAccount = '<account>';
```

Required: one row whose `Custody = '<custody>'`. **`v_ClientAccount` does NOT expose `Activated`
or `InitialDate`** (verified against the live view — selecting them returns HTTP 400, not a SQL
error); if you must confirm activation, read `Global.ClientAccount.Activated` from the base
table. Echo the resolved `(ClientAccount, Client, Custody, Currency, ApplyGrossUp, Offshore)`
back to the user. If the account isn't registered, hand off to the `ayunit_client` workflow —
finish that first.

**Custody must match, and it's load-bearing.** If the account resolves under a *different*
custody than the holdings (a real, common gap — e.g. `ClientAccount` registered under `UBS`
while `CustodyPosition` rows carry `UBS Miami`), **stop and reconcile before seeding.** It's not
cosmetic: `AccountPosition_Update`'s `I` branch computes `AccountFx` by looking up
`Global.ClientAccount WHERE ClientAccount=@Account AND fk_CustodyID=@Custody`'s `fk_CurrencyID` —
a custody that doesn't match the registration resolves to NULL, so `AccountFx` silently falls
back to `1.0`. Harmless for a same-currency (e.g. USD/USD) account, but it will mis-value every
cross-currency holding. Surface the mismatch and have the user fix the registration (or confirm
the correct custody) first.

**1b. NO existing `AccountPosition` rows — check the VIEW first (covers the common case):**

```sql
SELECT COUNT(*) AS n,
       MIN(Date) AS first_date,
       MAX(Date) AS last_date
FROM Portfolio.v_AccountPosition
WHERE Account = '<account>';
```

Required: `n = 0`. If `n > 0`, the chain is already seeded — **stop**, show the user the date
window and ask what they actually want to do (re-seed? backfill a missing day? amend a prior
inception?).

**1c. NO existing `AccountPosition` rows — also check the BASE TABLE (catches hidden NULL-FK rows):**

```sql
SELECT COUNT(*) AS n_base,
       SUM(CASE WHEN ap.fk_AssetID    IS NULL THEN 1 ELSE 0 END) AS n_null_asset,
       SUM(CASE WHEN ap.fk_CustodyID  IS NULL THEN 1 ELSE 0 END) AS n_null_custody,
       SUM(CASE WHEN ap.fk_CurrencyID IS NULL THEN 1 ELSE 0 END) AS n_null_currency
FROM Portfolio.AccountPosition ap
JOIN Global.ClientAccount ca ON ca.pk_ClientAccountID = ap.fk_ClientAccountID
WHERE ca.ClientAccount = '<account>';
```

Required: `n_base = 0`. Any hit here that step 1b missed means orphaned base-table rows
(`fk_AssetID`/`fk_CustodyID`/`fk_CurrencyID` NULL) — a data-quality issue that **must** be
cleaned up before inception, never seeded around. Surface the counts to the user.

**1d. NO `CheckedDate` rows whatsoever for `(Account, Custody)` — active OR inactive:**

```sql
SELECT pk_CheckedDateID, Account, Custody, Date, Activated, InputDate, InputUser
FROM Portfolio.v_CheckedDate
WHERE Account = '<account>' AND Custody = '<custody>'
ORDER BY Activated DESC, Date DESC;
```

Required: 0 rows. **Even an `Activated = 0` row is a red flag** — it means the pair was locked
before and someone deactivated it. That's not an inception; investigate. If you see a row,
stop and surface it (`pk`, `Date`, `Activated`, `InputDate`, `InputUser`).

**1e-A. Every held `Asset` resolves in `Global.Asset` (existence, not activation):**

Build the holdings list (next step), then validate:

```sql
SELECT Asset, Activated
FROM Global.v_Asset
WHERE Asset IN ('<a1>', '<a2>', '<a3>', …);
```

Required: **one row per held asset** (so the proc resolves `@Asset → fk_AssetID` and the seeded
row survives `v_AccountPosition`'s INNER JOIN). **Missing rows** → register via the
**`asset-register`** skill, then come back. **Never seed with an unresolved asset**
(`fk_AssetID = NULL` lands on the base table and the view's INNER JOIN drops it silently).

**`Activated = 0` is NOT an automatic stop — check *why*, judging as of the inception date.**
`Activated` is a *current lifecycle* flag ("is it still worth pulling prices/market data for
today?"), not a *was-this-valid-on-`Date`* flag. An inception is a back-dated snapshot, so an
instrument that has since matured / redeemed / been called / expired is correctly deactivated
today yet was a perfectly live holding on the (past) inception date — **it must be seeded.**
Two facts make this safe, both verified against the live contract:
- the proc's FK resolution is `Global.Asset WHERE Asset=@Asset` — it does **not** filter on
  `Activated`, so the insert resolves `fk_AssetID` normally; and
- `v_AccountPosition` INNER-JOINs `Global.v_Asset`, which **surfaces deactivated rows** (it has
  no `Activated` filter) — so the seeded row stays visible and base-count = view-count holds.

So bucket each `Activated = 0` hit by cause before deciding:
- **Matured / redeemed / called / expired on or after `Date`** (typical for a back-dated seed —
  e.g. a Treasury bill maturing weeks later) → **valid, seed it.** Worth a one-line note to the
  user ("B012726 is `Activated=0` today because it matured 2026-01-27, but was live on the
  2025-12-31 inception — seeding it"). The forward pipeline rolls it from `D+1` on its existing
  price history until it redeems to cash; deactivation today doesn't erase that history.
- **Erroneous / duplicate / wrongly-registered** → **fix first** (reactivate, merge, or correct
  the registration via `asset-register`), then seed. Don't seed a known-bad asset row.

When in doubt about an `Activated = 0` asset, surface it to the user with its maturity/why and
let them confirm — don't silently drop it (that would under-seed the book) and don't silently
seed a genuinely-bad one.

**1e-B. No `AssetCustody`-mapping gap for the held tickers** (informational; triggers §2a, doesn't stop):

When the holdings come from `v_CustodyPosition` (the common case), count how many rows for
this `(Account, Date)` still have `Asset IS NULL` — i.e. the custodian sent a `TickerCustody`
that `Portfolio.AssetCustody` doesn't yet map to a canonical `Asset`:

```sql
SELECT COUNT(*) AS n_unresolved,
       SUM(CASE WHEN IsinR IS NOT NULL OR AnbimaCodeR IS NOT NULL THEN 1 ELSE 0 END) AS n_with_identifier
FROM Portfolio.v_CustodyPosition
WHERE Account = '<account>' AND Date = '<inception_date>' AND Asset IS NULL;
```

- `n_unresolved = 0` → straight to §2 → §3.
- `n_unresolved > 0` → **§2a handles this inline** (resolve identifier → propose
  `AssetCustody_Update I` → preview → confirm → `Update_Missing_Asset` to back-fill
  history). Distinct from 1e-A: here the asset already exists in `Global.Asset`; only the
  custody-side mapping is missing. If the resolution loop in §2a can't find a `Global.Asset`
  for a given `AssetR`, *then* it hands off to `asset-register` (the 1e-A path).

**1f. Lookup values exist in their lookup tables:**

- `Currency` ∈ `Global.Currency` (examples: `BRL, USD, EUR, GBP, AUD, CAD, CHF, CNH, CNY, HKD, RUB`).
- `Custody` ∈ `Global.Custody`.
- `PriceSourceClose` values ∈ `Global.PriceSource`. **Don't trust a hardcoded list — query it
  live.** Beyond the firm-wide sources (`Anbima, BBG, BTG, Exchange, MS, Santander, XP`), there
  are **custody-named feeds** (e.g. `UBS Miami` is a registered `Source`), and a custody extract
  lifted from `v_CustodyPosition` will carry exactly that custody-named source — which is valid.
  Validate the actual `Source` values you're about to write with a `SELECT DISTINCT`, don't
  reject them for not being in the short list.

Run one `SELECT DISTINCT` per column in doubt; refuse the inception only if a value is genuinely
absent from its lookup table.

**Pre-flight summary report.** After all checks pass, report back to the user in a single
compact block: `ClientAccount` + `Client` + `Custody` + `Currency`, inception `Date`, asset
count, "no prior AccountPosition (view: 0, base: 0)", "no prior CheckedDate". Only then proceed.

### 2 — Gather the holdings (one row per held asset)

The grain is **`(Date, Account, Asset)`**. Source of truth on day 0 is normally the custody
extract for the inception date. If it's already in `CustodyPosition`, lift it from the view —
the canonical `Asset` is pre-resolved:

```sql
SELECT Asset, Currency,
       Quantity                       AS QuantityClose,
       Price                          AS PriceClose,
       Value                          AS ValueClose,
       ValueAfterTaxes                AS ValueCloseAfterTaxes,
       Source                         AS PriceSourceClose,
       COALESCE(PriceDate, Date)      AS PriceDateClose
FROM Portfolio.v_CustodyPosition
WHERE Account = '<account>' AND Date = '<inception_date>' AND Asset IS NOT NULL
ORDER BY Asset;
```

Untranslated rows (`Asset IS NULL`) are handled inline by **§2a** below — don't skip ahead;
the inception batch must enter §3 with every holding resolved to a canonical `Asset`. If the
user is providing holdings outside `CustodyPosition` (manual list, another system's export),
accept it but apply the same validations and skip §2a if every row already has an `Asset` code.

### 2a — Resolve untranslated `AssetR` rows in-flow (only when §1e-B counted > 0)

If §1e-B counted `n_unresolved > 0`, one or more holdings from `v_CustodyPosition` still have
`Asset IS NULL` — the custodian sent a `TickerCustody` that `Portfolio.AssetCustody` doesn't
map yet. **Read [`references/assetcustody-resolution.md`](references/assetcustody-resolution.md)
and run that sub-flow now**, before §3. It walks the full loop: detect → resolve `AssetR` →
canonical `Global.Asset` by identifier → preview the proposed `AssetCustody_Update I` mappings
→ confirm → insert → back-fill history with `Update_Missing_Asset` (triple-scoped to
`(Date, Account, Custody)`) → verify zero remain. Misses (asset not in `Global.Asset` at all)
route to the `asset-register` skill.

If `n_unresolved = 0` — or the user supplied holdings outside `CustodyPosition`, every row
already carrying an `Asset` code — skip this entirely and go straight to §3. The inception
batch must enter §3 with **every** holding resolved to a canonical `Asset`; never proceed with
an `Asset IS NULL` row still present.

### 3 — Apply the inception defaults

Expand each holding into the full param set. The shape is steady-state: `Open = Close`, zero
flows, no P&L.

- **Mirroring**: `QuantityOpen = QuantityClose`, `PriceOpen = PriceClose`,
  `PriceSourceOpen = PriceSourceClose`, `PriceDateOpen = PriceDateClose`,
  `ValueOpen = ValueClose`, `ValueOpenAfterTaxes = ValueCloseAfterTaxes`.
- **Zero flows**: `QuantityTransaction = 0`, `ValueTransaction = 0`, `PnlExGrossUp = 0`,
  `PnlGrossUp = 0`, `PnlDaily = 0`, `DailyReturn = 0`, `SellIncomeTaxes = 0`.
- **Defaults when unknown** (override only with explicit user input):
  - `AcquisitionDate = Date` — override with the real first-buy date if migrating ITD history.
  - `AvgPrice = PriceClose` — override with the imported cost basis if migrating from another
    system. After day 0 the pipeline only updates `AvgPrice` from new BUYs.
  - `QuantityLending = 0`, `QuantityMargin = 0` — override if the custody split is provided
    (both are **subsets** of `QuantityClose`, not additions).
  - `Mtd = Ytd = Itd = 0` — override with migrated cumulative returns if provided.
- **Cross-currency (the proc computes the FX — you don't):** if `Currency ≠ AccountCurrency`,
  you *may* pass `AccountCurrency = ClientAccount.Currency` and an `AccountFx` for your own
  preview math, but the `I` branch **ignores both and recomputes them server-side** (see "What
  the procedure actually persists" above): `AccountCurrency` ← the pair's `ClientAccount.Currency`,
  `AccountFx` ← `AssetCurrency_USD / AccountCurrency_USD` from `AssetData.v_Price` on `Date`.
  So the FX you reconcile against is whatever the price history yields — verify it in §6/§8
  rather than trusting the value you sent. For same-currency holdings the proc returns `1.0`.
- **Informational params** (`Description`, `Product`, `AssetClass`, `AssetGroup`, `Client`):
  the proc accepts them but does **not** store them on `I` — the view derives them from the
  Asset/Account FK. Passing them is fine for completeness; don't rely on them persisting.
- **`Activated = 1`** always.

**Special shapes:**
- **Cash / FX rows** (`AssetGroup = 'Cash'`): `Asset` = the currency code; `QuantityClose =
  ValueClose = ValueCloseAfterTaxes`; `PriceClose = AvgPrice = 1`. (The proc's `PI` branch also
  forces `PriceClose = 1` for `AssetGroup = 'Cash'` — same convention.)
- **Bonds / debentures / CRI-CRA**: custody `Value` includes accrued interest — store it
  verbatim, don't try to back out accruals at inception.
- **Empty inception** (account starts with no holdings): no position rows to write; skip §6–7
  and §9 entirely and go straight to §8 — advise the user that there are no inception positions
  and that they should add the `CheckedDate` (alone) to start the chain.

### 4 — Sanity gate (per row, before drafting)

- `ValueClose ≈ QuantityClose × PriceClose × ContractSize` within ~1% (rounding/accrual). A
  100× gap means a `ContractSize` issue on the asset registration — fix there, not here.
- `|ValueCloseAfterTaxes| ≤ |ValueClose|` (net ≤ gross).
- Sign of `Quantity` matches the position type (repos / CPRs / liabilities are negative).
- `AvgPrice` and `PriceClose` are in the asset's `Currency`, not the account's.

### 5 — Preview to the user (mandatory, every time)

Show the **full inception batch** as a table — every row, every column actually populated.
Don't truncate; the user is reviewing a one-shot seed. Include per-row notes (imported cost
basis, off-`Date` price, non-zero migrated ITD, a matured/`Activated=0` holding being seeded).
Translate `Activated` (`0/1` → não/sim or no/yes) and echo the resolved `(Account, Custody,
Date)` plus the asset count. State plainly what's about to happen — **and that the skill will
not create the CheckedDate** — then **end with an explicit confirm question** — e.g.:

> *"Abaixo estão as N linhas de inception que vou inserir em `Portfolio.AccountPosition` (uma por
> ativo, canary primeiro). `AccountFx`/`AccountCurrency` são recalculados pelo servidor a partir
> da moeda da conta + histórico de preços, então a FX no verify pode diferir da enviada. Cada
> chamada é uma transação isolada — não há `BEGIN TRAN` em volta; se algo falhar no meio, eu
> paro e ofereço rollback. **Não vou criar o `CheckedDate`** — depois que as posições estiverem
> validadas, eu te aviso e você adiciona o lock manualmente. **Está tudo correto? Confirma a
> inserção?**"*

(EN: *"Below are the N inception rows I'll insert into `Portfolio.AccountPosition` (one per
asset, canary first). … **I will not create the `CheckedDate`** — once the positions are
validated I'll tell you, and you add the lock manually. **Is everything correct? Do you confirm
the insert?**"*)

**Wait for an explicit yes — no write on implied approval, silence, or a vague reply.**
- **On an explicit yes** → proceed to §6 (canary), then §7, then verify + reconcile (§9), then
  the §8 handoff.
- **If the user asks for any change** (a price, a quantity, an `AcquisitionDate`/`AvgPrice`
  override, dropping or adding a holding) → revise the draft and **re-preview the full batch**;
  get a fresh yes against the corrected table. Never write a half-agreed batch.
- **If the user is unsure or wants to check custody** → stay read-only; offer to run the §8
  reconciliation against `v_CustodyPosition` so they can eyeball the match before committing.

### 6 — Canary (insert ONE position, verify, ask before continuing)

Pick the largest position by `|ValueClose|` as the canary — it's the most visible if anything
is wrong. (It may well be an `Activated = 0` instrument — e.g. the biggest line is often a
soon-to-mature Treasury that's since matured; per §1e-A that's fine to seed and the canary will
still appear in `v_AccountPosition`.) Send **only the minimal inception field set** below —
these are the fields that actually matter for a seed (verified live against `AccountPosition_Update@I`):

```
execute_procedure(
  database  = "AgnesOrg00DB",
  procedure = "Portfolio.AccountPosition_Update",
  cmd       = "I",
  params    = {
    "Date":"<inception_date>", "Account":"<account>", "Custody":"<custody>",
    "Asset":"<asset>", "Currency":"<ccy>", "AcquisitionDate":"<acq>",
    "QuantityOpen":<q>, "QuantityClose":<q>, "QuantityTransaction":0,
    "QuantityLending":0, "QuantityMargin":0,
    "PriceSourceOpen":"<src>", "PriceSourceClose":"<src>",
    "PriceDateOpen":"<pdate>", "PriceDateClose":"<pdate>",
    "AvgPrice":<p>, "PriceOpen":<p>, "PriceClose":<p>,
    "ValueTransaction":0, "ValueOpen":<v>, "ValueClose":<v>,
    "ValueOpenAfterTaxes":<vat>, "ValueCloseAfterTaxes":<vat>,
    "SellIncomeTaxes":0,
    "PnlExGrossUp":0, "PnlGrossUp":0, "PnlDaily":0, "DailyReturn":0,
    "Mtd":0, "Ytd":0, "Itd":0
  }
)
```

**Why only these.** This is the complete, sufficient param set for an inception `I` — the same
fields as the canonical `EXEC` template. Deliberately omitted, and why:
- `AccountCurrency` / `AccountFx` — **recomputed server-side on `I`** (proc header:
  *"Aceito; recomputado internamente"*). Sending them is a harmless no-op; the persisted values
  come from the account's currency + USD price history, so leave them out and verify the
  server's result in the re-SELECT below.
- `Activated` — **not in the `I` INSERT column list** (the base table has no `Activated`
  column). Pure no-op; omit it.
- `Product` / `AssetClass` / `AssetGroup` / `Client` — informational only, not persisted on `I`;
  the view re-derives them from the Asset/Account FK. Omit them.

A successful call returns `status = "success"` with `rowcount = -1` and **no `pk`** — the proc
doesn't return the new id. So **don't expect a `pk` in the response**; instead re-SELECT the row
to get its `pk_AccountPositionID` and confirm it persisted:

```sql
SELECT pk_AccountPositionID, Asset, QuantityOpen, QuantityClose, QuantityTransaction,
       PriceClose, AvgPrice, ValueClose, ValueCloseAfterTaxes, PnlDaily, DailyReturn,
       AcquisitionDate, AccountCurrency, AccountFx
FROM Portfolio.v_AccountPosition
WHERE Account = '<account>' AND Date = '<inception_date>' AND Asset = '<asset>';
```

Show the user the persisted row and confirm:
- Exactly **one** row comes back (not zero → FK resolution worked and the view's INNER JOIN
  didn't drop it; not >1 → no duplicate, see the idempotency note in §7).
- The values you control match: quantities, prices, `ValueClose`, after-tax, flows = 0,
  `AcquisitionDate`, `AvgPrice`.
- **`AccountFx`/`AccountCurrency` reflect the server's computation, not your input** — confirm
  they're *sensible* (same-currency → `1.0`; cross-currency → roughly the `Date` FX), not that
  they echo what you sent. A surprising FX here points to a missing currency price in
  `AssetData.v_Price` for `Date`, not a bug in your call.

**Pause for explicit approval before proceeding with the rest of the batch.** This is the only
safe checkpoint before committing the bulk of the seed.

### 7 — Batch the remaining positions (track every outcome)

Loop the remaining holdings, one `execute_procedure` per asset, with the same minimal param
shape as the canary. Keep a per-row outcome ledger from the start —
`(asset, status, pk_AccountPositionID, error?)`. **Stop on the first failure** (don't try to
plough through), and go to §9 (rollback).

> **⚠️ `@CMD='I'` is NOT idempotent — guard every insert with an existence check.**
> `AccountPosition` has **no unique constraint** on `(Date, Account, Asset)` and the `I` branch
> does a blind `INSERT` with no pre-check. Re-running the same `I` (a retry, an accidental
> double-send, a re-invoked skill) silently stacks **duplicate** rows — the seed grain is one
> row per `(Date, Account, Asset)`, and duplicates inflate every future day's position. Before
> **each** `I` (canary included), confirm the row isn't already there:
> ```sql
> SELECT COUNT(*) AS n FROM Portfolio.v_AccountPosition
> WHERE Account = '<account>' AND Date = '<inception_date>' AND Asset = '<asset>';
> ```
> `n = 0` → safe to insert. `n ≥ 1` → **skip and warn the user** (the asset is already seeded);
> never insert a second row. After each insert, the re-SELECT from §6 doubles as the
> post-check: it must return exactly **one** row for that asset, not two.

A success returns `status = "success"` with `rowcount = -1` and **no `pk`** in the response
(the proc doesn't return the new id — re-SELECT to get it, as in §6). A failure returns an
error — common ones:

| Error | Cause | Fix |
|---|---|---|
| `'<X>' not in Global.Currency` / `Global.Custody` / `Global.PriceSource` | Lookup string doesn't exist | Re-check pre-flight §1f; correct the value. |
| `fk_AssetID could not be resolved` (or row missing from `v_AccountPosition` after insert) | `Asset` code genuinely not in `Global.Asset` → `fk_AssetID = NULL` on the base table, dropped by the view's INNER JOIN. (Note: `Activated = 0` does **not** cause this — `v_Asset` surfaces deactivated rows, so a matured asset still appears.) | Pre-flight §1e-A should catch a missing code; if it slipped, register via `asset-register`. |
| `Insert proibido: posicao e anterior ao CheckedDate.` | An active `(Account, Custody)` `CheckedDate` exists at `Date ≥ inception_date` | Pre-flight §1d should catch this; if a lock was created between pre-flight and now, abort. |
| duplicate row appears (no error, but §6 re-SELECT returns 2+ rows for one asset) | `I` ran twice for the same `(Date, Account, Asset)` — no constraint stops it | Delete the extra via `@CMD='D' @pk_AccountPositionID=<dup pk>`; add the §7 existence pre-check if it was skipped. |
| any constraint violation | Sign / scale / unknown lookup | Inspect the row, fix or escalate. |

### 8 — Verify & reconcile the positions (after the full batch is in)

Only when the full position batch is in (canary + batch, all status = success), confirm the
seed end-to-end **before** telling the user to lock it. Two checks: structural integrity, then
reconciliation against custody.

```sql
-- All inception rows present, FKs resolved (view INNER-JOINs; missing FK → row absent),
-- and no duplicates per asset
SELECT Asset, COUNT(*) AS n,
       MAX(QuantityClose) AS QuantityClose, MAX(PriceClose) AS PriceClose,
       MAX(ValueClose) AS ValueClose, MAX(AvgPrice) AS AvgPrice,
       MAX(AccountFx) AS AccountFx
FROM Portfolio.v_AccountPosition
WHERE Account = '<account>' AND Date = '<inception_date>'
GROUP BY Asset
ORDER BY ValueClose DESC;

-- Base-table count matches view (no orphan NULL-FK rows). NOTE: the base table stores
-- Account as an nvarchar column directly — there is NO fk_ClientAccountID. Filter on Account.
SELECT COUNT(*) AS n_base
FROM Portfolio.AccountPosition
WHERE Account = '<account>' AND Date = '<inception_date>';
```

Sanity checklist:
- One row per asset (`n = 1` everywhere — no duplicates; the `I` branch has no unique
  constraint, so a double-send would show `n = 2` here, see §7).
- View row-count = base-table `n_base` = batch size (a mismatch means a row landed with NULL FK
  on the base table; the view hides it but the chain is still wrong — roll back and investigate).
- On every inception row: `QuantityOpen = QuantityClose`, `ValueTransaction = 0`, `PnlDaily = 0`,
  `DailyReturn = 0`.
- `AccountFx` is sane per row (same-currency → `1.0`; cross-currency → ≈ the `Date` rate). A
  `1.0` on a foreign-currency row means the currency had no price in `AssetData.v_Price` on
  `Date` — surface it; the seed FX feeds every future day's cross-rate.

Then **reconcile against the custody snapshot** for the inception date — the seed must match
custody or every future P&L carries the gap:

```sql
SELECT COALESCE(ap.Asset, cp.Asset) AS Asset,
       ap.ValueClose AS seed_value, cp.Value AS custody_value,
       ROUND(ISNULL(ap.ValueClose,0) - ISNULL(cp.Value,0), 2) AS diff
FROM (SELECT Asset, ValueClose FROM Portfolio.v_AccountPosition
      WHERE Account='<account>' AND Date='<inception_date>') ap
FULL OUTER JOIN (SELECT Asset, Value FROM Portfolio.v_CustodyPosition
      WHERE Account='<account>' AND Custody='<custody>'
        AND CAST(Date AS date)='<inception_date>' AND Asset IS NOT NULL) cp
  ON ap.Asset = cp.Asset
ORDER BY ABS(ISNULL(ap.ValueClose,0) - ISNULL(cp.Value,0)) DESC;
```

Every row should show `diff = 0.00` and neither side should be NULL (a NULL `seed_value` = a
custody holding you didn't seed; a NULL `custody_value` = a seeded row with no custody backing).
Surface any non-zero diff or NULL to the user; do **not** advance to the §8 handoff until the
reconciliation is clean (or the user explicitly accepts a known, explained gap).

### 9 — Mid-batch failure → rollback (only fires if §7 stops on an error)

Because `execute_procedure` calls are not wrapped in a transaction together, a failure at
position `k` of `N` leaves rows `1 … k-1` committed and `k … N` pending. **Do not just retry
the failed row** — surface the failure to the user and offer two options:

1. **Roll back what's in** — delete the committed rows so the chain is genuinely empty again:
   ```
   execute_procedure(
     database  = "AgnesOrg00DB",
     procedure = "Portfolio.AccountPosition_Update",
     cmd       = "D",
     params    = { "pk_AccountPositionID": <pk> }
   )
   -- repeat for every pk captured in the outcome ledger
   ```
   Then re-verify with `SELECT COUNT(*) FROM Portfolio.v_AccountPosition WHERE Account = '<account>'` — must be 0 — before re-trying the full inception. (`D` is lock-guarded, but since this skill never creates a `CheckedDate`, no lock can block the rollback on the happy path. If the *user* already added a lock for this pair, they must deactivate it via `CheckedDate_Update @CMD='U' @Activated=0` before the deletes will run.)

2. **Keep what's in, fix the offending row, finish the batch** — only if the failure was a
   single bad row (typo / missing FK), the user confirms, and the rest of the batch is verified
   untouched. Then go to §8 (verify) as normal. Risky; the rollback path is cleaner.

Because this skill never writes the `CheckedDate`, a partial seed is never accidentally frozen
— but a partial seed is still wrong. Resolve it (roll back, or fix-and-finish) and pass §8's
verify + reconcile **before** the §8 handoff. Never advise the user to add the lock on top of a
batch that didn't fully land and reconcile.

### 10 — Hand off the `CheckedDate` to the user (the skill does NOT write it)

Once §8's verify + reconcile is clean, the positions are seeded correctly but **not yet frozen**.
The skill stops writing here. Tell the user plainly:

1. The inception positions are inserted and validated — state the `(Account, Custody, Date)`,
   the asset count, the total `ValueClose`, and that the custody reconciliation came back clean
   (`diff = 0`).
2. The seed is **not frozen yet** — the pipeline will not take over from `D+1` until a
   `Portfolio.CheckedDate` exists for this `(Account, Custody)` at the inception date.
3. **They must add that `CheckedDate` themselves.** Give them the exact statement to run, and
   make clear it's deliberately left to them (the lock is audit-sensitive — it should be a
   conscious human action, not an automated side effect).

Provide the ready-to-run SQL, filled with the resolved values:

```sql
-- Freeze the inception seed — run in SSMS (or your normal CheckedDate tooling)
EXEC [Portfolio].[CheckedDate_Update]
     @Account   = N'<account>',
     @Custody   = N'<custody>',
     @Date      = '<inception_date>',
     @Activated = 1,
     @CMD       = 'I';
```

Once they confirm they've added it, you may (read-only) verify the lock landed:

```sql
SELECT Account, Custody, Date, Activated, InputDate, InputUser
FROM Portfolio.v_CheckedDate
WHERE Account = '<account>' AND Custody = '<custody>' AND Activated = 1;
```

Expected: one active row at `Date = <inception_date>`. After that, the seed is frozen and the
pipeline computes forward from `D+1`. **Do not call `CheckedDate_Update` yourself** — only
confirm, after the user has run it, that the lock is present.

## Critical rules

- **Refuse to proceed if pre-flight (§1) finds anything** — any `AccountPosition` row (view OR
  base), any `CheckedDate` (active OR inactive), an unresolved asset, an unknown lookup value.
  This is the most-asked-for safety: a "real" inception is the *very first* record for the pair.
  (Note: §1e-B counts unmapped `AssetR` rows for information — that's not a stop, it routes to
  §2a; it's stop-worthy only if §2a's verify (vi) leaves untranslated rows behind.)
- **Scope `Update_Missing_Asset` narrowly — always pass `@Date`, `@Account`, `@Custody`.**
  The CMD is a WRITE despite the read-only-sounding name: it back-fills `fk_AssetID` on every
  matching `CustodyPosition` row. Without all three filters it back-fills across the entire
  firm, silently mutating accounts and dates outside this inception's scope. §2a step (v) is
  the only place this skill calls it; that call is always triple-scoped.
- **This skill never writes the `CheckedDate`** — it seeds positions only, then advises the user
  to add the lock (§10). The lock is audit-sensitive and deliberately left as a human action.
  Don't call `CheckedDate_Update` with any CMD; only *read* `v_CheckedDate` (pre-flight §1d,
  verify the user's lock in §10).
- **Canary one, pause for approval, then batch** — the §6 checkpoint exists because the calls
  aren't atomic. It's the only chance to catch a systemic problem before committing the bulk.
- **Track every outcome** — `(asset, status, pk, error?)` from row 1. On the first failure,
  stop and go to §9; don't continue.
- **Guard every `I` with an existence check** — `AccountPosition` has no unique constraint on
  `(Date, Account, Asset)`; a re-run silently stacks duplicates. SELECT-before-insert (§7), and
  the §8 verify confirms exactly one row per asset.
- **Open = Close, flows = 0, P&L = 0** on every inception row. Deviations from the defaults
  (`AcquisitionDate`, `AvgPrice`, `Mtd/Ytd/Itd`) require explicit user input — never invent them.
- **Don't expect `AccountFx`/`AccountCurrency` to echo your input** — the `I` branch recomputes
  them from the account currency + USD price history. Verify they're sane, not that they match.
- **Resolve every asset and lookup value before drafting** — an unresolved `Asset` lands
  `fk_AssetID = NULL` on the base table and the row vanishes from `v_AccountPosition` silently.
- **Preview → confirm → execute → hand off** — show the full batch (§5), get an explicit yes,
  *then* write the positions (§6→§7), verify + reconcile (§8), then advise the user to add the
  lock (§10). No writes on silence or implied approval; any requested change means re-preview
  and a fresh yes.
- **Reconcile against custody before the handoff** — if the seed doesn't match
  `v_CustodyPosition` for the inception date, every future P&L carries the error; don't advise
  the lock until the reconciliation is clean (§8).
- **Reply in the user's language** (PT/EN) and echo the resolved `(Account, Custody, Date)`.

## When unsure

- **§1 finds prior `AccountPosition` rows** → this is **not** an inception. Surface what's there
  (date window, asset count, latest InputDate) and ask what the user is actually trying to do.
- **§1 finds an `Activated = 0` `CheckedDate`** → the pair was previously locked and someone
  deactivated it. Don't assume it's safe to seed over; show the `pk`, `Date`, `InputUser`,
  `InputDate` and ask.
- **§1c finds orphan base-table rows (NULL FK)** → a data-quality issue. Don't seed around it;
  surface the counts and escalate.
- **A held asset isn't in `Global.Asset` yet** → hand off to the `asset-register` skill,
  register it, then come back. Never seed with `fk_AssetID = NULL`.
- **A held asset is in `Global.Asset` but `Activated = 0`** → don't reflexively block (that
  under-seeds the book) and don't reflexively seed (might be a bad row). Judge as of the
  inception date: if it matured / redeemed / expired on or after `Date`, it was live then —
  seed it with a one-line note. If it's deactivated because the registration is wrong/duplicate,
  fix it (`asset-register`) first. See §1e-A.
- **Untranslated `Asset IS NULL` rows in `v_CustodyPosition`** → go through §2a (in-flow
  resolution): try identifier lookup → propose `AssetCustody_Update I` for matches → hand off
  `asset-register` for misses → back-fill historical rows with `Update_Missing_Asset` scoped
  to `(Date, Account, Custody)`. Never proceed to §3 with any unresolved row remaining.
  ([`reconciliation`](ayunit://docs/position/reconciliation) covers the `AssetR → Asset`
  bridge model when an `AssetR` is ambiguous.)
- **Canary verify (§6) shows a column wrong** → don't continue the batch. Delete the canary
  (`@CMD='D' @pk_AccountPositionID=…`), fix the param, re-run §6. (A "wrong" `AccountFx` is
  usually a missing currency price, not a bad param — check `AssetData.v_Price` first.)
- **Batch fails at row `k` of `N`** → §9. Don't try to plough through; resolve the partial seed
  (roll back, or fix-and-finish) and pass §8 verify + reconcile before the §10 handoff. Because
  the skill never writes the lock, a partial seed is never frozen — but it's still wrong, so
  don't advise the user to lock it until it's whole and reconciled.
- **Importing a cost basis or migrated cumulative returns** → set `AvgPrice`, `AcquisitionDate`,
  `Mtd/Ytd/Itd` from the user-supplied values on the inception row. This is the only chance —
  after `D+1` the pipeline only updates `AvgPrice` from new BUYs.
- **Multi-custody onboarding** → it's multiple `ClientAccount`s in practice; repeat this skill
  per `(Account, Custody)` pair, each with its own `CheckedDate`.
- **User wants to roll back a committed inception after pipeline has run forward** → confirm
  twice; rolling back inception is rolling back the whole chain. Deactivate the `CheckedDate`
  via `CheckedDate_Update @CMD='U' @Activated=0`, then `AccountPosition_Update @CMD='D'` per
  `pk`. The cascade in downstream tables (`Share`, lookthrough, etc.) is the user's call.

## Appendix — copy-paste `EXEC` fallback

When MCP / REST isn't available (SSMS-only context, audit-mandated single-block run), emit
the equivalent transactional `EXEC` block as documented in
[`ayunit://docs/position/inception`](ayunit://docs/position/inception) §"The EXEC block".
Same order (positions first, then `CheckedDate`), same `BEGIN TRAN` / `XACT_ABORT ON` /
`COMMIT` / `ROLLBACK` envelope. The in-MCP path above is the default; the EXEC fallback is for
contexts where it isn't an option.
