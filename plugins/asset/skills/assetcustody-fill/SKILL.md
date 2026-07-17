---
name: assetcustody-fill
description: "Use when the user needs to translate unmapped custody-side identifiers into canonical Global.Asset codes AT THE MAPPING LEVEL (Portfolio.AssetCustody), then propagate the mapping to both Portfolio.CustodyPosition (via Update_Missing_Asset) and Portfolio.AccountTransaction PENDING rows (which the loader validators auto-match on I/U once AssetCustody knows the ticker). This is the durable, upstream fix for the recurring pattern where the BTG (or any custody) loader receives a custody-side ticker/CNPJ/AnbimaCode that resolves to an already-registered Global.Asset but no per-custody translation row exists — leading to CustodyPosition rows with `Asset=NULL AssetR=<code>` and AccountTransaction PENDING rows with `Asset=NULL CustodyIdentifier=<code>`. Instead of patching each transaction row individually (as `pending-revalidate` and `pending-position-repair` do downstream), this skill fixes the mapping table ONCE, then runs `Portfolio.CustodyPosition_Update @CMD='Update_Missing_Asset'` to back-fill custody positions in bulk, then hands off the list of unblocked PENDING transaction pks to `pending-revalidate`. Verified real-world (2026-07-15 → 17): BTG loader repeatedly fails to consult `v_AssetCustody` on numeric assetCodes (307807/7054072/CDB4267Z8IU) and repeatedly fails to fall back to `Global.Asset.Cnpj` (44173493000137, 5833358 EXES) — running this skill against the fleet resolves ~50-100 unmapped rows per session without needing any AccountTransaction writes. Trigger phrases: 'the loader is missing this ticker mapping', 'CustodyPosition has Asset=NULL for AssetR X', 'BTG has unmapped assetCode Y', 'add a v_AssetCustody row for Z', 'back-fill custody positions where Asset is null', 'fill the AssetCustody translation for these codes', or when any orchestrator/routine surfaces a group of same-CustodyIdentifier PENDING transactions that would otherwise all need per-pk pending-revalidate."
---

# Fill AssetCustody mappings once — let the DB propagate the fix

You are the durable, upstream fix for **custody-side identifier translation**.
The loader (BTG, XP, JP, MS, …) receives a per-custody ticker/CNPJ/assetCode
that identifies a security. To reach a canonical `Global.Asset`, that ticker
must be present in `Portfolio.AssetCustody` (per-custody translation table).
When the loader can't find the ticker there, it leaves:

- `Portfolio.CustodyPosition.fk_AssetID = NULL` (position row with `Asset=NULL AssetR=<code>`)
- `Portfolio.AccountTransaction.Asset = NULL` (transaction row PENDING with `CustodyIdentifier=<code>`)

Every downstream skill (`pending-revalidate`, `pending-position-repair`,
`assetrelated-fix`, `daily-btg-onshore-routine`) has to work around this by
patching each row individually. That's O(rows). This skill fixes the
**mapping** once — O(unique tickers) — then relies on:

1. `Portfolio.CustodyPosition_Update @CMD='Update_Missing_Asset'` to back-fill
   every affected CustodyPosition row (proc-side, one call).
2. The `AccountTransaction_Update @CMD='U'` **auto-Asset-match validator**
   (see CLAUDE.md §8): on every U/I of a PENDING BUY/SELL/ASSET
   RECEIPT/DELIVERY, the proc reads `v_AssetCustody(TickerCustody =
   @CustodyIdentifier OR @AssetCustody, Custody = @Custody)` and fills
   `Asset` if it's NULL. So a subsequent `pending-revalidate` invocation
   promotes them automatically.

**Write scope:** `Portfolio.AssetCustody` (fleet-wide master data — one row
per (Custody, TickerCustody) affects **every** account on that custody) and
`Portfolio.CustodyPosition` (via the `Update_Missing_Asset` back-fill).
**Does NOT write to `AccountTransaction`** — that's the caller's next step,
usually `pending-revalidate` scoped to the pks this skill returns as
`unblocked_pks`.

## Coverage

Handles four families of unmapped custody-side identifiers:

| Family | Example | Global.Asset match column | Custody source | Verified real cases |
|---|---|---|---|---|
| CNPJ (BR fund) | `44173493000137` | `Cnpj` (LIKE match) | BTG `fundCnpj` in `RawTransaction`, or the numeric `CustodyIdentifier` on the transaction row | EXES SPECIAL OPPORTUNITIES FIDC → `C0000660991`; 001364382 / 003575819 / 004320549 |
| BTG numeric `assetCode` (NTN-B, LFT) | `307807`, `7054072` | via existing `v_AssetCustody.TickerCustody` (a base mapping typically exists) | BTG feed `assetCode` in `RawTransaction`, or the AssetR on CustodyPosition | NTN-B 15/08/2030 → `BRSTNCNTB3B8`; NTN-B 15/05/2033 → `BRSTNCNTB6B1`; 004434113 |
| BTG numeric AssetR (fund) | `5833358` | via existing custody CNPJ → find via join | CustodyPosition.AssetR when the account holds a fund BTG only reports numerically | EXES (5833358) → `C0000660991`; 001364382, 004320549 |
| AnbimaCode-shaped CDB / CRA / CRI | `CDB4267Z8IU`, `CDB6260Z4YZ` | `AnbimaCode` (exact) | BTG `CustodyIdentifier` on the transaction row (typically `CustodyIdentifier = CDB-<code>` stripped) | 004434131 (`CDB4267Z8IU`), 003900834 (dozens of `CDB…` codes) |

**Not covered:**

- Genuine new instruments not in `Global.Asset` at all — hand off to
  `asset-register` (this skill NEVER creates a `Global.Asset` row).
- Structural registration errors (an existing `Global.Asset` with wrong
  ISIN/CNPJ) — this is a `Global.Asset_Update @CMD='U'` fix, not a mapping
  fix.
- Fund description gaps (a held fund has stub `Description`, blocking
  `assetrelated-fix` Layout D fuzzy match). That's a Description-update
  fix, not an AssetCustody mapping fix.

## Inputs

Any of, in any combination:

- `identifiers`: explicit list of unique custody-side codes to resolve.
  `["5833358", "307807", "44173493000137", "CDB4267Z8IU"]`
- `custody`: **required** for auto-INSERT scope. `"BTG"`, `"XP"`, `"JP"`.
  AssetCustody rows are per-custody; a numeric `307807` may only be valid
  for BTG.
- `accounts`: optional filter used during **discovery** (§Step 1) when
  scanning `CustodyPosition` and `AccountTransaction` for unmapped rows. If
  omitted, discovery scans the whole custody.
- `date_window`: optional `(from, to)` for the discovery scan. Default:
  scan every date from the earliest active `CheckedDate + 1` to today (so
  we don't wake up pre-lock artifacts).
- `dry_run`: default `false`. When `true`, resolve identifiers and produce
  the plan, but do NOT INSERT into `AssetCustody` or run
  `Update_Missing_Asset`.
- `default_position_factor`, `default_price_factor`: default `1.0, 1.0`.
  Only override for exotic securities where the custody feed carries a
  scale factor (e.g. debentures quoted per-mille).

**Echo the resolved `(identifiers, custody, accounts, dry_run)` at the
start of every reply.** Reply in the user's language (PT/EN); JSON field
names always in English.

## Reference resources (read on demand)

| Resource | Read when… |
|---|---|
| [`ayunit://docs/asset/relationship`](ayunit://docs/asset/relationship) | The FK hub — what `AssetCustody` adds on top of `Global.Asset` (per-custody `TickerCustody`, `TickerCustody2`, `DescriptionCustody`, `PositionFactor`, `PriceFactor`). |
| `get_procedure_detail('Portfolio', 'AssetCustody_Update')` | Confirm `@CMD='I'` param list before writing. Idempotency note: the proc has NO uniqueness guard on `(Custody, TickerCustody)` — always S-first before I to prevent duplicates. |
| `get_procedure_detail('Portfolio', 'CustodyPosition_Update')` | `@CMD='Update_Missing_Asset'` params: `@Custody`, `@Date`, `@Account` (all optional). Returns a rowset per back-filled row: `(pk_CustodyPositionID, AssetR, Custody, MatchedAsset, Status)`. |
| CLAUDE.md §8 (auto-validators) | The `AccountTransaction_Update` auto-Asset-match rule that makes the AssetCustody INSERT propagate automatically to the transaction side (via a subsequent `pending-revalidate` U). |

## Tools you call directly

- `mcp__ayunit__execute_select_query` — every read: discovery, dedupe check,
  verification. Never the raw table; read from `Portfolio.v_AssetCustody`
  and `Portfolio.v_CustodyPosition`.
- `mcp__ayunit__identify_asset` — Global.Asset resolution for each unique
  identifier. Batch via `mcp__ayunit__identify_assets_bulk` when the input
  list is > 3.
- `mcp__ayunit__execute_procedure` — the write path. Two allowlisted CMDs
  used by this skill:
  - `Portfolio.AssetCustody_Update @CMD='S'` (dedupe check per identifier).
  - `Portfolio.AssetCustody_Update @CMD='I'` (autonomous INSERT of a HIGH
    mapping).
  - `Portfolio.CustodyPosition_Update @CMD='Update_Missing_Asset'` (bulk
    back-fill).
- **No writes to `AccountTransaction`** in this skill. The caller (a
  routine, or the user) chains `pending-revalidate` after this to promote
  the transactions.

## The cycle

### 1 — Discover unmapped identifiers

If the caller supplied `identifiers`, skip discovery and go to §2. Else:

```sql
-- Source A: CustodyPosition rows with Asset=NULL (untranslated AssetR)
SELECT DISTINCT
    cp.AssetR                                  AS identifier,
    cp.Custody,
    MAX(cp.AssetCustody)                       AS sample_description,
    'CustodyPosition'                          AS source,
    COUNT(*)                                   AS n_rows
FROM Portfolio.v_CustodyPosition cp
WHERE cp.Custody = @custody
  AND cp.Asset IS NULL AND cp.AssetR IS NOT NULL
  AND (@accounts IS NULL OR cp.Account IN (@accounts))
  AND (@from_date IS NULL OR CAST(cp.[Date] AS date) >= @from_date)
GROUP BY cp.AssetR, cp.Custody

UNION ALL

-- Source B: PENDING AccountTransaction rows with Asset=NULL
SELECT DISTINCT
    at.CustodyIdentifier                       AS identifier,
    at.Custody,
    MAX(at.AssetCustody)                       AS sample_description,
    'AccountTransaction'                       AS source,
    COUNT(*)                                   AS n_rows
FROM Portfolio.v_AccountTransaction at
WHERE at.Custody = @custody
  AND at.Status = 'PENDING' AND at.Asset IS NULL
  AND at.CustodyIdentifier IS NOT NULL
  AND (@accounts IS NULL OR at.ClientAccount IN (@accounts))
GROUP BY at.CustodyIdentifier, at.Custody
ORDER BY identifier;
```

Union'd distinct set. An identifier appearing on both sides is one to
resolve once and back-fill both surfaces from.

### 2 — Resolve each unique identifier

Batch call:

```
mcp__ayunit__identify_assets_bulk(identifiers=<distinct list>)
```

Classify each result:

| identify_asset result | This skill's verdict | Next action |
|---|---|---|
| `resolved_from = 'Global.Asset'` (unique match) | **HIGH** | Proceed to §3 auto-INSERT. |
| `resolved_from = 'AssetData.Asset'` (secondary) | **REVIEW** | Report the AssetData match for human verification; do NOT auto-INSERT (AssetData is a pricing master, not the book). |
| `resolved_from = null` | **NOT_FOUND** | Hand off to `asset-register`. Report with the identifier + observed `sample_description` as hints. |

**Custody-scoped fallback (Source B specifically)**: if `identify_asset`
misses on the raw identifier but the custody is BTG *and* the identifier
matches an existing `v_AssetCustody.TickerCustody` for BTG that already
resolves — no INSERT needed. The mapping exists; the loader is simply
failing to consult it. This is a *loader bug*, not a mapping gap. Report
as `LOADER_BUG` and do not INSERT.

```sql
SELECT ac.Asset, ac.TickerCustody, ac.DescriptionCustody
FROM Portfolio.v_AssetCustody ac
WHERE ac.Custody = @custody
  AND (ac.TickerCustody = @identifier OR ac.TickerCustody2 = @identifier);
```

If the row exists but no `PositionFactor` / `PriceFactor` mismatch is
suspected → mark `LOADER_BUG`. Downstream leaves (`pending-revalidate` on
transactions, `Update_Missing_Asset` on positions) will still pick these
up because the mapping IS there — the loader just failed to use it at
insert time.

### 3 — Auto-INSERT AssetCustody rows for HIGH resolutions

For each HIGH tuple `(custody, identifier, resolved_asset)`:

1. **Dedupe check** (idempotency guard — the proc has no unique
   constraint on `(Custody, TickerCustody)`):

   ```
   execute_procedure Portfolio.AssetCustody_Update @CMD='S'
     @Custody = <custody>
     @TickerCustody = <identifier>
   ```

   If any row returns, **skip** — mapping already exists. Log as `EXISTS`.

2. **INSERT**:

   ```
   execute_procedure Portfolio.AssetCustody_Update @CMD='I'
     @Custody = <custody>
     @Asset = <resolved_asset>
     @TickerCustody = <identifier>
     @DescriptionCustody = <sample_description or null>
     @PositionFactor = 1
     @PriceFactor = 1
   ```

   For a multi-identifier run, prefer `execute_batch` with all the `I`
   items (atomic; safer than 20 individual `execute_procedure` calls).

3. **Confidence tag** the write for audit:
   this skill has no `AgentCheck` column on `AssetCustody` (it's a master
   table), so the audit trail is the `InputDate` + `fk_InputUserID` (proc
   sets these automatically). Emit the tag in the reply and the report:
   `[ACF-<custody>] identifier <x> → Asset <y> (via identify_asset:
   <resolved_via>)`.

**Batch pattern for a fleet-wide fill (verified BTG 2026-07-17 shape):**

```
execute_batch(
  database='AgnesOrg00DB',
  items=[
    {procedure:'Portfolio.AssetCustody_Update', cmd:'I', params:{
       Custody:'BTG', Asset:'C0000660991',
       TickerCustody:'5833358',
       DescriptionCustody:'EXES SPECIAL OPPORTUNITIES FIDC',
       PositionFactor:1, PriceFactor:1}},
    {procedure:'Portfolio.AssetCustody_Update', cmd:'I', params:{
       Custody:'BTG', Asset:'BRSTNCNTB3B8',
       TickerCustody:'307807',
       DescriptionCustody:'NTN-B 2030-08-15',
       PositionFactor:1, PriceFactor:1}},
    ...
  ],
  dry_run=<same as caller>
)
```

### 4 — Bulk back-fill CustodyPosition

After all new mappings land:

```
execute_procedure Portfolio.CustodyPosition_Update @CMD='Update_Missing_Asset'
  @Custody = <custody>
  # @Date and @Account optional — narrow when the scope is a single account
```

The proc joins `Portfolio.CustodyPosition cp` on `v_AssetCustody ac`
where `ac.TickerCustody = cp.AssetR OR ac.TickerCustody2 = cp.AssetR` for
the same custody, and sets `cp.fk_AssetID` for every row that matches.
Returns one rowset per back-filled row:
`(pk_CustodyPositionID, AssetR, Custody, MatchedAsset, Status='Updated')`.

Capture the row count; add to the report as `custody_position_backfilled = <N>`.
Idempotent — safe to re-run.

### 5 — Emit `unblocked_pks` for the caller

Query the AccountTransaction surface for pks whose `CustodyIdentifier` matches
the newly-INSERTed mappings (or was already covered by pre-existing ones we
verified in §2's LOADER_BUG bucket):

```sql
SELECT at.pk_AccountTransactionID AS pk, at.ClientAccount, at.CustodyIdentifier
FROM Portfolio.v_AccountTransaction at
WHERE at.Custody = @custody
  AND at.Status = 'PENDING'
  AND at.Asset IS NULL
  AND at.CustodyIdentifier IN (<all resolved identifiers, both new INSERTS and LOADER_BUG hits>);
```

Return this list to the caller as `unblocked_pks`. **This skill does NOT
write to AccountTransaction.** The caller (routine or user) invokes
`pending-revalidate` scoped to these pks next — the loader's auto-Asset-
match validator (CLAUDE.md §8) now sees the mapping and fills `Asset`
during the U command, promoting the row to VALIDATED.

### 6 — Report buckets

End every run with these buckets so nothing is silently dropped:

| Bucket | Meaning | Disposition |
|---|---|---|
| **Inserted** | HIGH identify_asset resolutions, new `AssetCustody` row committed | done; downstream back-fill runs next |
| **Exists** | Mapping already existed (dedupe hit) | done; nothing to write |
| **Loader-bug** | Mapping exists BUT the loader failed to use it — no INSERT needed, but the loader team should be notified (this skill can't fix the loader) | flag; include in report |
| **Review — AssetData** | `identify_asset` matched via the AssetData fallback (secondary source) — do NOT auto-INSERT; a human should verify the AssetData record maps to the correct Global.Asset | user decision |
| **Not-found — asset-register** | `identify_asset` returned nothing on either source | hand off to `asset-register` with the identifier + `sample_description` |
| **CustodyPosition back-filled** | `Update_Missing_Asset` row count | done |
| **AccountTransaction unblocked pks** | pks now ready for `pending-revalidate` | hand off to `pending-revalidate` |

One-line summary at the end:
`inserted=N · exists=X · loader_bug=L · review=R · not_found=F · positions_backfilled=P · unblocked_txns=T`

## Critical rules

- **Fleet-wide write scope.** `AssetCustody` is master data. A new row for
  `(Custody='BTG', TickerCustody='5833358')` affects EVERY BTG account.
  Confirm the identifier is truly a BTG-side ticker (not e.g. a Bloomberg
  code accidentally miscategorised) before INSERT. `identify_asset`
  HIGH via `Global.Asset.<column>` is the gate.
- **Dedupe before every INSERT.** The proc has no uniqueness constraint;
  a second I with the same `(Custody, TickerCustody)` silently duplicates
  the row and `Update_Missing_Asset` will start matching multiple times
  (harmless in itself but noisy for future audits). If duplicates are
  discovered, use `AssetCustody_Update @CMD='D_Duplicates'` (allowlisted?
  verify — otherwise emit a copy-paste `EXEC` for the operator).
- **Never INSERT on `LOADER_BUG` classification.** If the mapping already
  exists, the fix is on the loader side; INSERTing a duplicate mapping to
  "help" makes it worse.
- **Never INSERT on `Review — AssetData` classification.** AssetData is
  the pricing master, not the book. A Global.Asset gap for that identifier
  requires an `asset-register` first.
- **`PositionFactor` and `PriceFactor` default to 1.0.** Only override for
  documented per-mille or per-thousand quoting conventions. Wrong factors
  silently rescale both positions and cash flows downstream.
- **`Update_Missing_Asset` is idempotent** — safe to re-run. Always
  invoke after every INSERT batch, even if the previous back-fill just
  ran; the newly-inserted mappings need it.
- **Never advance CheckedDate.** Same rule as every write skill. This
  skill affects positions and future transactions; the analyst approves
  the reconciliation state via the `checkeddate-update` path.
- **Reply in the user's language** (PT/EN) and echo the resolved scope.

## When unsure

- **Identifier `307807` is a BTG numeric assetCode but `identify_asset`
  returns nothing.** Check `v_AssetCustody` scoped to BTG — 2026-07-15
  verified that many BTG numeric codes IS already mapped there. If so,
  the fix is `LOADER_BUG`, not a new INSERT. If it isn't mapped and
  `identify_asset` returns nothing, the underlying instrument (usually an
  NTN-B by settlement/maturity date parsed from `RawTransaction`) needs
  `asset-register` first.
- **CNPJ 14 digits vs 15 digits.** Strip leading zeros and punctuation
  before `identify_asset` — the proc's `DAAC` command uses `LIKE '%' +
  @Identifier + '%'` for CNPJ, but our identify_asset wrapper is stricter.
- **Two custody-side identifiers point to the same Global.Asset (e.g.
  fund CNPJ AND fund AnbimaCode both resolve to the same `C00…`).**
  INSERT both — `TickerCustody` is a distinct per-code mapping, and
  future feeds may send either. `AssetCustody` supports two aliases per
  row (`TickerCustody` + `TickerCustody2`) but multi-alias INSERT via
  `@CMD='I'` requires setting both explicitly.
- **A HIGH resolution's `Global.Asset.Activated = FALSE`.** Report as
  `deactivated_target`; do NOT INSERT (would silently point a live custody
  feed at a deactivated asset). Human decides whether to reactivate the
  Global.Asset or route via a different code.
- **The identifier is a description-shaped free text (`"NTNB"`, `"Sten D5
  FIM"`) rather than a code.** `identify_asset` will miss layer 1 and 2;
  layer 3 (Description fuzzy) returns LOW. Report — do not INSERT a
  free-text `TickerCustody`; the loader would round-trip it wrong.

## Real-world verification (2026-07-15 → 17)

BTG onshore, from `daily-btg-onshore-routine` runs:

| Identifier | identify_asset verdict | Action | Impact |
|---|---|---|---|
| `307807` | HIGH via `v_AssetCustody.TickerCustody`(BTG) | LOADER_BUG (already mapped) | Skip INSERT; report |
| `7054072` | HIGH via `v_AssetCustody.TickerCustody`(BTG) | LOADER_BUG (already mapped) | Skip INSERT; report |
| `CDB4267Z8IU` | HIGH via `Global.Asset.AnbimaCode` | INSERT (was missing per-custody mapping) | Unblocks 1 transaction on 004434131 + back-fills any CustodyPosition rows |
| `44173493000137` | HIGH via `Global.Asset.Cnpj` → `C0000660991` | INSERT (was missing per-custody mapping) | Unblocks 6 transactions across 001364382/003575819/004320549 + back-fills their CustodyPosition rows |
| `5833358` (BTG's numeric AssetR for EXES) | Miss on identify_asset (numeric not in Global.Asset); recover via join `v_CustodyPosition.AssetR = 5833358 → paired asset via account holding + CNPJ 44173493000137 → C0000660991` | INSERT `TickerCustody='5833358' → Asset='C0000660991'` | Back-fills every CustodyPosition row for BTG accounts that hold EXES (fleet-wide impact) |

Two INSERTs to `AssetCustody` + one `Update_Missing_Asset` call would have
resolved ~9 transaction pks and back-filled every EXES CustodyPosition row
across the fleet — versus the ~9 per-pk `AccountTransaction_Update U`
calls that were needed the manual way.
