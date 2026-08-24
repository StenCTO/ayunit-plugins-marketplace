# BTG onshore feed payload → XP-equivalent categories

The XP inception recipe (`seed-build-conventions.md` §3) is written in
terms of eight asset kinds: funds / pension / fixedIncome / treasury /
stock-tradedFunds / COE / repo / cash. BTG's feed payload is shaped
differently — this file is the map.

**Mandatory reading on the FIRST account of a run.** Dump the full
response of `mcp__ayunit__get_btg_onshore_account_information` for that
account, walk each section, and confirm the mapping below matches. If a
section is missing / renamed / contains unexpected sub-fields, **stop
and surface the divergence to the analyst before scaling**. The BTG feed
schema changes across releases; this file may lag.

Cross-references:
- [`seed-build-conventions.md`](seed-build-conventions.md) — the per-kind
  seed recipes this mapping feeds.
- [`btg-identifier-quirks.md`](btg-identifier-quirks.md) — how to resolve
  each section's identifiers to a `Global.Asset` code.
- [`ayunit://docs/feeds/routing`](ayunit://docs/feeds/routing) — the
  `access_name` convention for the BTG onshore tools.

---

## 1. Two BTG feed tools — DIFFERENT purposes; do NOT confuse

Verified against the live MCP tool schemas 2026-08-20 — read carefully
before invoking either.

### 1.1 `mcp__ayunit__get_btg_onshore_account_information` — REGISTRATION only

Signature: `(access_name, account)`. **No date parameter.** Returns the
holder, co-holders, and authorised users registered on the account —
BTG's account-registration data. **Does NOT return positions or per-lot
facts.** Read-only against BTG; read-only against the DB.

Useful for: verifying holder identity, cross-checking `Global.ClientAccount`
metadata (Client, holder name, etc.). **Not useful for §Step 3.2 feed
pull** (per-lot facts live in the position feed, not here).

### 1.2 `mcp__ayunit__process_btg_onshore_position` — WRITES CustodyPosition

⚠️ **This tool writes to `Portfolio.CustodyPosition`.** Signature:
`(access_name, accounts: list, date)`. For each `(account, date)` it
first deletes every existing `CustodyPosition` row (`CMD='DCD'`), then
inserts the fresh positions fetched from BTG's API.

**On a historical cutoff (e.g. seeding 2026-06-30 on 2026-08-20),
calling this tool overwrites the already-loaded snapshot with whatever
BTG returns today** — which may differ (backfills, adjustments) and
almost certainly won't return the per-lot facts (`accY`, `originalDate`,
`purchaseDate`) that BTG only exposes for current-date requests. So on
historical inceptions this tool is at best a no-op and at worst
destructive to a validated custody snapshot.

**Rule for this orchestrator:**

- **If `cutoff_date` is close to today** (within the current business
  week AND BTG's feed API returns per-lot facts for that date) → OK to
  call for the FIRST tranche's canary account, to obtain per-lot facts
  for the fund `accY` / `originalDate` fields.
- **If `cutoff_date` is historical** (weeks/months behind) → **do NOT
  call `process_btg_onshore_position`**. Rely on the already-loaded
  `Portfolio.v_CustodyPosition` snapshot for quantities/prices/values.
  Fall back to the `seed-build-conventions.md` §3.1 **"invalid accY"**
  branch: `AvgPrice = seed Price`, `Itd = 0`, `AcquisitionDate =
  cutoff_date`. The analyst can back-fill cost basis manually later if
  needed.

**Verified on 003575819 @ 2026-06-30** (2026-08-20 test run): the
historical snapshot was already loaded (11 rows, R$5.63M), all assets
resolved, no need to call `process_btg_onshore_position`. The
"invalid accY" fallback produced a clean seed that reconciled to
`d_qty = 0` on all 11 assets against `v_CustodyPosition`.

---

## 2. Payload section → XP-kind mapping

The BTG payload's top-level keys (as of the last verified schema,
2026-08 — **re-verify on the first account of every new run**) map as
follows. Section names in `<code>` are BTG's; the "XP kind" column names
the recipe in `seed-build-conventions.md` §3 to apply.

| BTG section | XP kind | Notes |
|---|---|---|
| `investmentFunds` / `funds` | §3.1 Funds | One row per fund lot. Carries `cnpj`, `anbimaCode`, `quantity`, `nav` / `price`, `accY` / `accumulatedYield`, `originalDate` / `purchaseDate`, sometimes `irValue`. |
| `pensionPlans` / `previdencia` | §3.2 Pension | **One row per certificate** for the same fund CNPJ. Combine per CNPJ (weighted-avg accY, min originalDate) — see §3.2. |
| `fixedIncome` / `renda_fixa` | §3.4 Fixed income | One row per lot. Carries `code` (BTG numeric assetCode, becomes `TickerCustody`), `isin`, `cnpj` (issuer), `quantity`, `price`, `purchaseDate`, `maturity`, `indexer` (IPCA / CDI / PRE), `coupon`. |
| `structuredNotes` / `coe` | §3.5 COE | One row per lot. Carries `code`, `quantity`, `price`, `purchaseDate`, `maturity`. |
| `treasury` / `tesouro` / `tesouroDireto` | §3.6 Treasury | May appear split (Tesouro Direto retail vs secondary lots) — see the combine rule in §3.6. |
| `stocks` / `equities` | §3.3 Traded funds / equities | Ticker-matched. Carries `ticker`, `quantity`, `price`, sometimes `purchaseDate`. |
| `tradedFunds` / `etf` / `fii` | §3.3 Traded funds / equities | Same treatment (ticker match). FIIs land here or under `funds` depending on BTG's client-side classification; if under `funds` they still need §3.1 fund treatment — check `Global.Asset.AssetClass`. |
| `repo` / `compromissada` / `overnight` | §3.7 Repo | One row per repo contract. Carries `value`, `rate`, `term`. |
| `cash` / `caixa` / `saldo` | §3.8 Cash | Single row (or per-currency in a multi-currency account — BTG onshore is BRL-only in practice). Carries `currency`, `value`. |

**Anything not in this table** → escalate on the first account. Do not
silently drop unknown sections; BTG occasionally adds instrument types
(recently: crypto ETFs, offshore feeder shares) that need explicit
handling.

---

## 3. Per-lot fact transcription — required fields per kind

Transcribe every lot into `facts_<account>_<cutoff_date>.json`.
**Missing any field marked required** → escalate the account (fact
gap = seed uncertain).

### 3.1 Fund lot (required)

```json
{
  "kind": "fund",
  "cnpj":         "44173493000137",
  "anbimaCode":   "260010",             // optional; either identifier resolves
  "quantity":     123456.789,
  "price":        1.31965,              // fund NAV on cutoff
  "accY":         1.234567,             // > 1 for positive return; NULL/≤0 → treat as invalid per §3.1
  "originalDate": "2024-03-15",         // "0001-01-01" → cutoff_date
  "irValue":      3137.40                // present on come-cotas / redemption; NOT used in seed
}
```

### 3.2 Pension certificate (required)

```json
{
  "kind": "pension",
  "cnpj":               "12345678000199",
  "certificateNumber":  "CERT-000123",
  "quantity":           500.0,
  "accY":               1.185,
  "originalDate":       "2023-06-01"
}
```

### 3.3 Fixed-income lot (required)

```json
{
  "kind": "fi",
  "code":         "307807",              // BTG numeric assetCode → v_AssetCustody.TickerCustody
  "isin":         "BREXESDBS107",        // when present; use for match fallback
  "cnpj":         "44173493000137",      // issuer CNPJ (also a valid resolution target)
  "quantity":     100.0,
  "price":        4267.892,
  "purchaseDate": "2025-04-12",
  "maturity":     "2035-08-15",
  "indexer":      "IPCA",
  "coupon":       6.0                    // % pa
}
```

### 3.4 COE lot (required)

```json
{
  "kind": "coe",
  "code":         "COE-BTG-2027-XYZ",
  "quantity":     50.0,
  "price":        1050.00,
  "purchaseDate": "2024-11-01",
  "maturity":     "2027-11-01"
}
```

### 3.5 Treasury lot (required)

```json
{
  "kind": "treasury",
  "ticker":       "NTN-B 2035",          // Global.Asset.Bbg
  "isin":         "BRSTNCLTB021",        // fallback
  "quantity":     10.0,
  "price":        4321.50,
  "purchaseDate": "2024-01-15",
  "secondary":    false                  // true = secondary-market lot; false = Tesouro Direto retail
}
```

### 3.6 Equity / traded fund lot (required)

```json
{
  "kind": "equity",
  "ticker":       "PETR4",
  "quantity":     1000.0,
  "price":        32.50,
  "purchaseDate": "2024-08-20"           // often absent; fall back to cutoff_date per §3.3
}
```

### 3.7 Repo (required)

```json
{
  "kind": "repo",
  "value": 500000.00,
  "rate":  13.75,
  "term":  "overnight"                   // or a date; not used in seed
}
```

### 3.8 Cash (required)

```json
{
  "kind": "cash",
  "currency": "BRL",
  "value":    12345.67
}
```

---

## 4. Cross-checks against `v_CustodyPosition`

After transcription, **cross-check the facts JSON against
`Portfolio.v_CustodyPosition`** on cutoff. Any asset held per the feed
but missing from `v_CustodyPosition` (or vice versa) indicates the
custody loader hasn't yet ingested this snapshot cleanly. Options:

- **Feed has it, custody doesn't** → the custody load lag is behind the
  cutoff. Wait for the next custody load, OR run the loader manually,
  OR mark the account `status = "custody_load_lag"` and skip.
- **Custody has it, feed doesn't** → the reverse; usually a BTG feed
  API issue for that specific account. Escalate — do NOT seed the
  missing lot from `v_CustodyPosition` alone (the per-lot facts like
  `accY` and `originalDate` don't exist there).

Only when both sides agree per-asset should the seed build proceed.

---

## 5. AQUISIÇÃO VIRTUAL → APLICAÇÃO pattern (verify per tranche)

Per the draft's BTG-specific check: BTG onshore emits an AQUISIÇÃO
VIRTUAL row followed by an APLICAÇÃO row for the same event (a fund
purchase that's not yet settled). The custody snapshot sometimes reflects
one, both, or neither of them depending on load timing. If the account's
first-tranche accounts show this pattern in their post-cutoff PENDINGs,
handle per [`pendings-sweep-patterns.md`](pendings-sweep-patterns.md) —
the pair is a **structural duplicate** on the same economic event.

Only relevant to the pendings sweep in §Step 8; the seed itself uses the
`v_CustodyPosition` snapshot which reports the settled state.
