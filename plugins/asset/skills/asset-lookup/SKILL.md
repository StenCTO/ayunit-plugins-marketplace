---
name: asset-lookup
description: "Use when the user has one or more custody-side identifiers (ISIN, CUSIP, CNPJ, an ANBIMA / Fund / Class code, a numeric internal custody code like BTG's `assetCode`, a custody free-text description, or an unknown ticker) and needs to know the canonical `Asset` code that ties this instrument back to `Global.Asset` and — per custody — via `Portfolio.v_AssetCustody`. Read-only; the skill NEVER registers. It is the mandatory pre-check before `asset-register`: an analyst who says 'unknown BTG NTNB coming from the feed' almost always finds the NTN-B already registered (by ISIN) with a `v_AssetCustody` mapping (per Custody: BTG's numeric `TickerCustody`), and the real fix is a data / mapping issue — NOT a duplicate registration. Trigger on phrases like 'what asset is 44173493000137?', 'resolve this ISIN / CNPJ / CUSIP', 'this CDB — is it registered?', 'the loader failed to identify this asset — check the book', 'is CDB4267Z8IU in Global.Asset?', 'the BTG feed sent assetCode 307807, find the Asset', or when another skill (e.g. `assetrelated-fix`, `pending-position-repair`, `daily-btg-onshore-routine`) discovers a PENDING row with `Asset=NULL` and needs to decide between 'map an existing Asset in `v_AssetCustody`' vs 'hand off to `asset-register`'. PT/EN both fine."
---

# Look up an asset by any identifier — is it already in the book?

You are the read-only bridge between **custody-side identifiers** (whatever a
feed sends: ISIN, CUSIP, CNPJ, ANBIMA `C00…` code, ticker, CETIP code, BTG's
numeric `assetCode`, a Bloomberg ticker, a free-text description) and the
firm's canonical `Asset` code in `Global.Asset`. Every downstream write
(position, transaction, price) points at `Asset` via `fk_AssetID`, so
resolving a custody-side identifier to the right `Asset` **before** doing
anything else is the first defence against duplicate registrations and
mis-attributed positions.

This skill exists because the loader's own identification path is narrow
(it only looks at a few columns on each feed's row) and repeatedly fails
on identifiers that ARE already registered — verified real-world 2026-07-15
across accounts 001364382 / 004320549 / 004434113 / 004434131:

- BTG `assetCode=307807` — loader failed → but `Portfolio.v_AssetCustody.TickerCustody='307807'` for `Custody='BTG'` maps to `Asset='BRSTNCNTB3B8'` (NTN-B 15/08/2030 IPCA).
- BTG `assetCode=7054072` — loader failed → maps to `Asset='BRSTNCNTB6B1'` (NTN-B 15/05/2033 IPCA).
- BTG `CustodyIdentifier='CDB4267Z8IU'` — loader failed → registered as `Asset='CDB4267Z8IU'` directly (custody code == Asset code).
- BTG `fundCnpj=44173493000137` — loader failed → `Global.Asset.Cnpj='44173493000137'` resolves to `Asset='C0000660991'` (EXES SPECIAL OPPORTUNITIES FIDC).

In all four cases, `asset-register` would have been the **wrong** answer
(create a duplicate). The right answer was either a `v_AssetCustody` row that
already existed, or a `Global.Asset.Cnpj` match that the loader didn't try.
This skill runs that check reliably.

**Never registers.** The output is a verdict + evidence; if the verdict says
"not found", the caller (or the user) hands off to `asset-register`.

## Inputs

Any of, in any combination:

- A single identifier: `identifier: "44173493000137"` (any kind, unknown type OK).
- A batch: `identifiers: ["307807", "7054072", "CDB4267Z8IU"]`.
- A per-row structured payload from a loader/PENDING probe:
  `{ pk, custody, custodyIdentifier, assetCustody, rawAssetCode, description }`
  — the skill runs every non-null field through the resolution pipeline
  and returns a per-`pk` verdict.
- Optional custody context: `custody: "BTG"` — narrows the
  `v_AssetCustody` lookup to that feed. Highly recommended when the input
  is a custody-side numeric code (BTG's `assetCode`, XP's proprietary
  ticker) because those numeric codes are **not globally unique** — the
  same `307807` could theoretically appear as a different asset on a
  different custody's mapping. Without a custody filter, that resolution
  degrades to `AMBIGUOUS`.

Echo the resolved inputs at the top of every reply. **Reply in the user's
language (PT/EN).**

## Reference resources (read on demand)

| Resource | Read when… |
|---|---|
| [`ayunit://docs/asset/faq`](ayunit://docs/asset/faq) | Asset model overview (the two hierarchies, Offshore, TaxRegime). Read once when in doubt about what an `AssetGroup` means. |
| [`ayunit://docs/asset/relationship`](ayunit://docs/asset/relationship) | The FK hub — what `v_AssetCustody` adds on top of `Global.Asset` (per-custody mapping between an internal Asset and a feed's ticker/description). |
| `get_view_detail('Global.v_Asset')` / `get_view_detail('Portfolio.v_AssetCustody')` | Introspect the exact columns before writing a lookup query. Schema drift is real. |

## Tools you call directly

- `mcp__ayunit__identify_asset` — the primary Global.Asset match. Given one
  identifier of any kind, it runs `Global.Asset_Update @CMD='S_IdentifyAsset'`
  which searches `Asset`, `BbgCode`, `Isin`, `Cusip`, `AnbimaCode`, `FundCode`,
  `ClassCode`, `Cnpj`, `ExchangeCode`, `CetipCode`, `SelicCode`. Returns the
  full row when found, or `resolved_from=null` when not (do NOT guess).
- `mcp__ayunit__identify_assets_bulk` — batch version, same semantics, one
  call for many inputs. Prefer this whenever `len(identifiers) > 3`.
- `execute_select_query` — for the `Portfolio.v_AssetCustody` lookup that
  `identify_asset` does NOT do (per-custody mappings), and for the fuzzy
  `Global.v_Asset.Description` last-resort match.

**No write tools.** This skill never calls `execute_procedure`, `execute_batch`,
or any `*_Update` procedure.

## The resolution pipeline

Run in fixed order for each input identifier. **First hit wins** — later
stages don't fire if an earlier stage returns a canonical `Asset` code.

### 1 — Global.Asset direct match (highest confidence)

```
mcp__ayunit__identify_asset(identifier=<x>)
```

If `resolved_from = 'Global.Asset'`, return **HIGH** with the matched
`Asset` code and the column that hit (echo the full row in the evidence
so the user can eyeball). This covers: ISIN, CUSIP, CNPJ, ANBIMA code,
Fund/Class code, CETIP/SELIC code, Bloomberg code, exchange ticker, and
the canonical `Asset` code itself.

**Don't second-guess `identify_asset`.** If it returns `resolved_from=null`
with `row_count=0` and no error, treat it as authoritative — the input is
not indexed as any of Global.Asset's known identifier columns.

### 2 — Portfolio.v_AssetCustody per-custody mapping (HIGH when custody-scoped)

`identify_asset` does **not** search `Portfolio.v_AssetCustody`. That view
is the per-feed dictionary: for a given `Custody`, a `TickerCustody` /
`TickerCustody2` / `DescriptionCustody` maps to a canonical `Asset`. This
is where BTG's numeric `assetCode` lives, where XP's proprietary tickers
live, and where the loader **should** be looking (but sometimes isn't).

If a `custody` was provided:

```sql
SELECT Asset, Custody, TickerCustody, TickerCustody2, DescriptionCustody,
       PositionFactor, PriceFactor, CalcRule
FROM Portfolio.v_AssetCustody
WHERE Custody = @custody
  AND ( TickerCustody     = @identifier
     OR TickerCustody2    = @identifier
     OR DescriptionCustody = @identifier );
```

- Exactly one row → **HIGH**. Return `Asset` and the matched column.
- Multiple rows → **AMBIGUOUS** (real data problem in v_AssetCustody:
  same custody ticker maps to >1 asset — surface all rows, do not pick).
- Zero rows → fall through to stage 3.

If **no** custody was provided, run the same lookup across all custodies:

```sql
SELECT Custody, Asset, TickerCustody, TickerCustody2, DescriptionCustody
FROM Portfolio.v_AssetCustody
WHERE TickerCustody = @identifier
   OR TickerCustody2 = @identifier
   OR DescriptionCustody = @identifier;
```

- Exactly one Asset across all custodies → **HIGH-CROSS-CUSTODY** (all rows
  point to the same `Asset` — the identifier is globally unique for the
  book even without a custody filter). Return that `Asset` plus every
  matching `Custody` row as evidence.
- Different `Asset`s across custodies → **AMBIGUOUS**. Report all
  candidates and require the caller to supply `custody`. Do NOT guess.
- Zero rows → stage 3.

### 3 — Global.v_Asset description fuzzy match (LOW, last resort)

Only if stages 1 and 2 miss AND the input looks like free text (contains
spaces or non-alphanumeric characters), fuzzy-match the description
against `Global.v_Asset.Description`:

```sql
SELECT TOP 20 Asset, Description, AssetGroup, Activated
FROM Global.v_Asset
WHERE Description LIKE '%' + @cleaned_input + '%'
   OR Description LIKE '%' + @first_words + '%';
```

- Rank by longest common substring / normalised token overlap.
- Return the **top 3 candidates as `LOW`** — never HIGH. A description
  match is a hint for a human, not proof of identity. The caller decides.

If stage 3 also misses → **NOT_FOUND**. Recommend `asset-register` in the
verdict. Suggest which `identify_asset` fields the user should still try
(sometimes an operator has an ISIN they haven't quoted yet).

### 4 — Aggregate the verdict

For batch calls, return one entry per input identifier with:

```json
{
  "identifier":  "<echoed input>",
  "custody":     "<echoed input | null>",
  "verdict":     "HIGH" | "HIGH-CROSS-CUSTODY" | "AMBIGUOUS" | "LOW" | "NOT_FOUND",
  "asset":       "<canonical Asset code | null>",
  "resolved_via": "Global.Asset.<column>" | "Portfolio.v_AssetCustody.<column>" | "Global.v_Asset.Description(fuzzy)" | null,
  "evidence": { … full matched row(s) or top-3 fuzzy candidates … },
  "next_step":   "use <Asset> in the write"
              |  "supply custody to disambiguate"
              |  "hand off to asset-register with the following hints: {…}"
              |  "escalate — data conflict in v_AssetCustody"
}
```

Emit the per-input verdicts, then a one-line summary:
`resolved=N/M · not_found=X · ambiguous=Y · low=Z`.

## Critical rules

- **Read-only. No writes. Ever.** If the caller wants to insert or update,
  hand off to `asset-register` (for INSERT) or the appropriate mapping-fix
  skill (for `v_AssetCustody` maintenance). This skill's output feeds
  those; it never invokes them.
- **Trust `identify_asset` when it says no.** Global.Asset indexes 10+
  identifier columns. If none matched, do not silently fuzzy-match against
  `Description` in place of stage 1 — go through stages 2 and 3 in order.
- **Custody-scoped queries beat cross-custody ones.** A BTG `TickerCustody`
  is only guaranteed unique **within BTG**. When you have a custody
  context, use it.
- **`HIGH-CROSS-CUSTODY` is a real, tighter tier than `LOW`.** It means
  the identifier resolved to exactly one `Asset` code across every
  custody mapping — that IS the answer, no ambiguity. Don't demote it.
- **`AMBIGUOUS` is not `NOT_FOUND`.** An ambiguous result means the book
  has data (possibly correct, possibly conflicting). Surface every
  candidate row so the operator can pick or fix upstream. Never guess.
- **Description fuzzy match is LOW forever.** A name match is a lead, not
  proof. Even a 100% Description hit doesn't promote to HIGH — the
  operator confirms.
- **Report the `resolved_via` column, always.** "How did you find this?"
  is the first question an auditor asks — answer it in the verdict.

## When unsure

- **Identifier looks like a CNPJ but has 15+ digits** → strip punctuation
  and leading zeros before `identify_asset`; if still no hit, verify it's
  a Brazilian CNPJ shape (14 digits, `_.__.___/____-__` mask) — some
  feeds prefix the account or a check character.
- **The identifier is a custody free-text like `"NTNB"` or `"Sten D5 FIM"`**
  → these are generic labels, not identifiers. Layer 1 will miss; layer
  2 might hit `DescriptionCustody`; layer 3 will fuzzy-match against
  Description with many candidates. Return LOW + top-3 and prompt the
  caller to supply the CNPJ / ISIN / assetCode instead — a generic label
  cannot resolve to a unique bond.
- **A BTG `assetCode` returns NOT_FOUND** → check whether the loader is
  actually running the `v_AssetCustody` lookup at all; several 2026-07
  incidents traced back to the loader ignoring that view for BTG
  specifically. Note in the verdict that this is likely a loader bug,
  not a genuine missing registration.
- **Description fuzzy match returns a clearly-right single candidate**
  (score ≥ 0.95, one clear winner) → still return LOW. Do not promote.
  If the caller wants to promote it in downstream logic, that's their
  call. This skill's job is to hand over honest signals, not to make the
  decision.
- **The `v_AssetCustody` lookup returns 2 rows for the same custody with
  different `Asset` codes** → this is a data problem in the mapping
  table (someone registered the same custody ticker twice). Report both
  Asset codes as `AMBIGUOUS` and flag it as `escalate — data conflict in
  v_AssetCustody`. Do NOT try to fix here.
