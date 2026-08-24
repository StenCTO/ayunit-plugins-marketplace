# BTG onshore identifier quirks — resolution playbook

BTG onshore uses a set of identifier conventions that regularly trip up
the `AccountTransaction` / `CustodyPosition` loaders and produce
`Asset IS NULL` rows even when the underlying asset is already registered
in `Global.Asset`. This file catalogues the known quirks with verified
examples so the orchestrator's §Step 3.3 asset triage can resolve them
without spurious `asset-register` calls.

Cross-references:
- [`asset:asset-lookup`](../../../../asset/skills/asset-lookup/SKILL.md)
  — the read-only pre-check that resolves every identifier before any
  registration attempt.
- [`asset:assetcustody-fill`](../../../../asset/skills/assetcustody-fill/SKILL.md)
  — the upstream mapping-fix leaf; INSERTs the missing
  `Portfolio.AssetCustody` row and back-fills `CustodyPosition` via
  `Update_Missing_Asset`.
- Sibling orchestrator: `plugins/routines/skills/daily-btg-onshore-routine/SKILL.md`
  leaf H (`assetcustody-fill`) — the daily routine's version of the
  same fix.

---

## 1. The recurring failure mode

The BTG loader inserts `Portfolio.AccountTransaction` and
`Portfolio.CustodyPosition` rows carrying the custody-side identifier
(`AssetCustody`, `CustodyIdentifier`, `AssetR`) but fails to consult
`Portfolio.v_AssetCustody` for the per-custody `TickerCustody` mapping.
Result: `Asset IS NULL` on both tables even when a `Global.Asset` row
exists for that identifier.

The failure is **structural to the loader**, not to the master data. The
correct fix is:

1. `asset-lookup` resolves the identifier → confirms `Global.Asset`
   already has a row.
2. `assetcustody-fill` INSERTs the missing `Portfolio.AssetCustody`
   translation row (per-custody: `Custody='BTG'`, `TickerCustody=<what
   BTG sent>`, `Asset=<canonical code>`).
3. `Portfolio.CustodyPosition_Update @CMD='Update_Missing_Asset'`
   (invoked by `assetcustody-fill`) back-fills every affected
   `CustodyPosition` row.
4. `pending-revalidate` on the affected `AccountTransaction` pks
   promotes them to `VALIDATED`.

**Never** hand off to `asset-register` before running `asset-lookup`.
Verified 2026-07-15: 3 of 4 candidate "unregistered" assets on that run
were in fact already registered — the mapping was missing.

---

## 2. Verified BTG identifier patterns

### 2.1 Numeric `assetCode` as `TickerCustody`

BTG's internal `assetCode` is a plain integer (e.g. `307807`, `7054072`,
`5833358`) that lands as the `TickerCustody` value in
`Portfolio.v_AssetCustody`. It is **not** an ISIN, CETIP, or ANBIMA
code — it's BTG's internal instrument id.

**Verified cases:**

| BTG `assetCode` | Resolves to | Instrument | Fix |
|---|---|---|---|
| `307807` | NTN-B 2035 (or similar Treasury) | Treasury | `AssetCustody I` with `TickerCustody='307807' Custody='BTG'` → `Update_Missing_Asset` |
| `7054072` | (varies — resolve via ISIN cross-check) | Fund | Same pattern |
| `5833358` | EXES (fund) | Fund | Same |

`asset-lookup` catches these by cross-checking `TickerCustody` against
`v_AssetCustody`; if no hit, it falls back to `Global.Asset.Cnpj` /
`Isin` matches. When the loader failed to resolve `307807`, running
`asset-lookup(identifier='307807', custody='BTG')` typically returns
HIGH with the canonical Asset code — proving the master data was there
all along.

### 2.2 CNPJ fallback (BR-fund identifiers)

BTG frequently sends the fund's CNPJ (14-digit numeric) as the
`AssetR` / `CustodyIdentifier` instead of the ANBIMA code. The loader
doesn't always fall back to `Global.Asset.Cnpj` when the direct
`TickerCustody` lookup misses.

**Verified cases:**

| BTG identifier | Fund | Fix |
|---|---|---|
| `44173493000137` | EXES fund | `asset-lookup` matches on `Global.Asset.Cnpj='44173493000137'` |
| (many BR-fund CNPJs) | various | Same pattern |

**Resolution order in `asset-lookup` for a numeric identifier:**

1. Strip punctuation. If the result is 14 digits → treat as CNPJ.
2. Match `Global.v_Asset` on `Cnpj = <stripped>` (single-hit → HIGH).
3. Match `Portfolio.v_AssetCustody` on `TickerCustody = <raw>` AND
   `Custody = 'BTG'` (single-hit → HIGH; verifies the mapping doesn't
   already exist elsewhere).
4. Fall back to ANBIMA-code shape match (`Global.v_Asset.AnbimaCode`)
   for 6-digit-ish numerics.

### 2.3 CDB / LCI / LCA structured code

BTG uses codes like `CDB4267Z8IU` (letter-numeric mixed) for CDBs, LCIs,
and LCAs. These land as `AssetR` / `AnbimaCode` on the custody side and
`AssetCustody` / `CustodyIdentifier` on the transaction side.

**Verified case:**

| BTG code | Instrument | Fix |
|---|---|---|
| `CDB4267Z8IU` | CDB (Certificado de Depósito Bancário) | `asset-lookup` matches on `Global.Asset.AnbimaCode='CDB4267Z8IU'` |

The code is BTG's internal structured identifier, not a public code, so
match only on `AnbimaCode` in `Global.Asset` (which the analyst
registered under BTG's convention).

### 2.4 Fund class code vs ANBIMA code

BR funds have a **fund CNPJ** (fund-level) and a **class code**
(class-level). BTG may send either. `asset-lookup` should try both:

- `Global.v_Asset.Cnpj` → the fund-level match.
- `Global.v_Asset.ClassCode` → the class-level match.
- `Global.v_Asset.AnbimaCode` → the ANBIMA canonical code.

Because a fund and its class can be registered as separate `Global.Asset`
rows (or as one row keyed by the class), a lookup that returns >1
candidate means the analyst has to disambiguate — never auto-pick.
Mark the account `status = "asset_disambiguation_needed"` and continue.

### 2.5 Ticker mismatches (equities / FIIs)

Rare on onshore, but BTG occasionally sends the base ticker (`PETR`)
instead of the traded class (`PETR4`). `asset-lookup` should try:

- Exact `Global.Asset.Bbg` match.
- Exact `Global.Asset.ExchangeCode` match.
- Numeric-suffix stripping (`PETR4` → `PETR`) as a last resort with
  MEDIUM confidence (flag for analyst review).

---

## 3. When `asset-lookup` returns NOT_FOUND

The identifier genuinely doesn't match anything in `Global.Asset` /
`v_AssetCustody`. **Only then** hand off:

- CNPJ-shaped identifier → `asset:register-br-funds` (ANBIMA-enriched
  chain).
- Any other kind → `asset:asset-register` with peer analogy (equity,
  bond, CDB, treasury, etc.).

If `asset-register` refuses (no confident peer set), mark the account
`status = "blocked_asset_register"` and skip.

---

## 4. AQUISIÇÃO VIRTUAL → APLICAÇÃO duplicate pattern

Not strictly an identifier quirk, but a related BTG loader pattern worth
capturing here for the pendings sweep step:

BTG onshore emits an `AQUISIÇÃO VIRTUAL` transaction (BTG's internal
provisioning for a fund purchase that's not yet settled), followed by an
`APLICAÇÃO` transaction (the settled purchase) once T+1 clears. The
custody snapshot then reflects the settled state (one lot, per the
APLICAÇÃO), but the loader may have ingested both transactions with
`Status = 'VALIDATED'` — creating a **structural duplicate on the same
economic event**.

Detection (see [`pendings-sweep-patterns.md`](pendings-sweep-patterns.md)
for the full treatment):

- Two `AccountTransaction` rows on close dates (usually the same day
  or T+1), same `(Account, Custody, Asset via AssetRelated cross-match,
  ABS(Quantity), ABS(Value))`.
- One has `GeneralLedgerDescription` containing `AQUISIÇÃO VIRTUAL` (or
  `AQ VIRTUAL`).
- The other has `GeneralLedgerDescription` containing `APLICAÇÃO`.

Fix: IGNORE the AQUISIÇÃO VIRTUAL row (it's the provisioning artifact,
not the settled event). Route through
`account-transaction:duplicate-trade-reconcile` in the sweep step.

---

## 5. Debugging a stuck identifier

If `asset-lookup` returns UNKNOWN / LOW on an identifier that "clearly
looks" like it should resolve, run the diagnostic ladder in order:

```sql
-- 1. Direct v_AssetCustody probe (per-custody mapping)
SELECT Custody, TickerCustody, TickerCustody2, Asset, PriceFactor, PositionFactor
FROM Portfolio.v_AssetCustody
WHERE Custody = 'BTG'
  AND (TickerCustody = '<id>' OR TickerCustody2 = '<id>');

-- 2. Global.Asset probe on every identifier column
SELECT Asset, Description, AssetGroup, Cnpj, Isin, Cusip, AnbimaCode,
       ClassCode, Bbg, ExchangeCode, Ticker
FROM Global.v_Asset
WHERE Cnpj        = '<id>' OR Isin       = '<id>' OR Cusip = '<id>'
   OR AnbimaCode  = '<id>' OR ClassCode  = '<id>'
   OR Bbg         = '<id>' OR ExchangeCode = '<id>'
   OR Ticker      = '<id>' OR Asset      = '<id>';

-- 3. AccountTransaction historical probe (has any load ever resolved it?)
SELECT TOP 20 pk_AccountTransactionID, Date, Status, Asset, AssetCustody,
       CustodyIdentifier, GeneralLedgerDescription
FROM Portfolio.v_AccountTransaction
WHERE Custody = 'BTG'
  AND (AssetCustody = '<id>' OR CustodyIdentifier = '<id>')
ORDER BY Date DESC;
```

The three probes together answer: is it mapped per-custody? does the
canonical row exist? has anything ever loaded it correctly? If all
three miss, it's genuinely new — hand off to the register path.
