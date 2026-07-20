---
name: new-asset-onboarding
description: "Use when the user wants to sweep recently-inserted Portfolio.AccountTransaction rows across ALL custodies for assets not yet in Global.Asset or not yet mapped in Portfolio.AssetCustody, and onboard them end-to-end — register the security in Global.Asset, add the per-custody translation row, bulk-backfill Portfolio.CustodyPosition, unblock PENDING trades, and backfill historical prices in AssetData.Price. This is an **orchestrator skill** (meta-skill): it does NOT write to the DB directly; it re-reads the four sub-detector SQL queries from account-transaction:transaction-workday-audit Check 1 (1a needs-registration / 1b needs-mapping-only / 1c needs-position-backfill / 1d needs-price-backfill) as its own detection phase — per the no-loop reciprocity convention, it does NOT invoke that audit skill via the Skill tool — then chains the leaf skills asset:asset-lookup (safety pre-check on 1a tuples), asset:register-br-funds + asset:asset-register (registration paths), asset:assetcustody-fill (mapping + CustodyPosition backfill via Update_Missing_Asset), account-transaction:pending-revalidate (promote unblocked PENDINGs), and asset:asset-price-history (price backfill). Runs autonomously — for Brazilian fund CNPJs the register-br-funds → asset-register chain; for other kinds (equities, options, bonds, offshore funds, FIIs, treasuries) invokes asset-register with peer-analogy classification (copies FK conventions — Issuer, AssetClass, Product, Benchmark, Source, TaxRegime — from existing assets of the same SecurityType). Ambiguous cases the orchestrator cannot safely classify are logged, not guessed. Reports go to disk AND are uploaded to Azure Blob via mcp__ayunit__upload_blob_file. Designed to run BEFORE the daily custody routines so they find zero unmapped assets. Supports optional per-account scoping via `account_filter` (ad-hoc mode: applies to every detector query, skips the daily state lock, writes report under `reports/ad-hoc/`) so the audit's Check 1 executing form can invoke it for a single account's gaps without a fleet-wide sweep. Fires on prompts like 'onboard new assets from this week', 'register any unknown assets in the last 3 days', 'sweep unmapped assets', 'run the new-asset onboarding routine', 'roda o new-asset-onboarding para os últimos 3 dias', 'cadastra os ativos novos que apareceram nos trades recentes', 'roda o onboarding para a conta X'."
---

# `new-asset-onboarding` — sweep, register, map, backfill

You are the orchestrator for Sten's **asset-master gap sweep**. Custody
loaders (BTG onshore/offshore, XP, UBS Miami, JPM, MS, …) periodically drop
`Portfolio.AccountTransaction` rows referencing instruments that are not yet
in `Global.Asset` or not yet mapped per-custody in `Portfolio.AssetCustody`.
Those trades sit as `PENDING` with `Asset = NULL` and never reach
`AccountPosition` (only `VALIDATED` / `UPDATED` feed positions — see
[`ayunit://docs/transaction/faq`](ayunit://docs/transaction/faq),
[`ayunit://docs/portfolio-creator/pipeline`](ayunit://docs/portfolio-creator/pipeline)).

This skill sweeps a rolling window across every custody, resolves every
new-asset gap by chaining the asset-plugin skills end-to-end, and produces a
per-run JSON + markdown report that is also uploaded to the ayunit Azure Blob
store for downstream tooling. It is **custody-agnostic** and **autonomous**:
the two decisions it defers to a human are (a) truly ambiguous
classifications where `asset-register` refuses to guess, and (b) advancement
of any `CheckedDate` — that lives on the specialist path (see
[`ayunit://docs/checkeddate/usage`](ayunit://docs/checkeddate/usage)).

This is a **meta-skill / orchestrator**: it never writes to
`Portfolio.AccountTransaction`, `Global.Asset`, `Portfolio.AssetCustody`, or
`AssetData.Price` directly. Every mutation goes through a leaf skill whose
own `SKILL.md` owns the guardrails.

## Positioning in the daily routine chain

Run this **before** the daily custody routines (`daily-btg-onshore-routine`,
`daily-btg-offshore-routine`). By the time they enumerate their late
accounts, this skill has already:

- registered every new asset that appeared in the window,
- inserted the per-custody `AssetCustody` mapping,
- back-filled `Portfolio.CustodyPosition` via `Update_Missing_Asset`,
- promoted the affected `PENDING` transactions to `VALIDATED`,
- backfilled historical prices for the newly-registered assets.

The custody routines then find zero unmapped assets and can focus purely on
position reconciliation. The two routines never race — this skill's own
state lock and each custody routine's own state lock are independent
per-day locks.

## Leaf skills it invokes (in order)

| # | Skill | Purpose |
|---|---|---|
| 1 | `account-transaction:transaction-workday-audit` (Check 1 detector SQL only) | Source of the four onboarding-gap detector queries: **1a** unmapped tuples where no `Global.Asset` identifier matches (needs registration), **1b** unmapped tuples where a `Global.Asset` row DOES match (needs the per-custody mapping row only), **1c** `Portfolio.v_CustodyPosition` rows in the audited scope with `Asset=NULL AssetR=<code>` (needs `Update_Missing_Asset` back-fill), **1d** registered+mapped assets whose `AssetData.v_Price` coverage from earliest trade date to today has gaps. **Read only the SQL in that skill's Check 1 sub-detectors — do NOT invoke `transaction-workday-audit` recursively** (see "Reciprocity convention" below). |
| 2 | `asset:asset-lookup` | Safety pre-check on every candidate tuple. Catches audit false-negatives — assets already in `Global.Asset` under an identifier the audit's LEFT JOIN didn't reach. |
| 3a | `asset:register-br-funds` → `asset:asset-register` | Autonomous chain for Brazilian fund CNPJs. `register-br-funds` resolves the CNPJ to the ANBIMA classe/subclasse code, pulls the ANBIMA cadastral registry, hands a pre-filled payload to `asset-register` for the INSERT. |
| 3b | `asset:asset-register` (peer-analogy) | For every non-BR-fund unknown (equity, option, bond, FII, treasury, offshore fund). Copies FK conventions from existing assets of the same `SecurityType` cluster (Issuer, AssetClass, Product, Benchmark, Source, TaxRegime), validates every FK against live lookup tables, previews, INSERTs via `Global.Asset_Update @CMD='I'`. Refuses to guess when no confident peer set exists — those go to `ambiguous_assets`. |
| 4 | `asset:assetcustody-fill` | For every registered Asset (plus every "already registered but not mapped" case from Step 2's `known_by_lookup` bucket): INSERT `Portfolio.AssetCustody` row, run `Portfolio.CustodyPosition_Update @CMD='Update_Missing_Asset'` to bulk-backfill `CustodyPosition`, return the `unblocked_pks` list. |
| 5 | `account-transaction:pending-revalidate` | Fed the aggregated `unblocked_pks` from Step 4. Re-runs `Portfolio.AccountTransaction_Update @CMD='U'` on each; the auto-Asset-match validator now sees the fresh mapping and promotes PENDING → VALIDATED. |
| 6 | `asset:asset-price-history` | Per newly-registered Asset. Window `[MIN(trade.Date from Step 1 audit), today]`. Walks its 4-source priority chain (MarketData → AssetDataDB.v_Price → `Portfolio.v_CustodyPosition × PriceFactor`) — the CustodyPosition fallback is exactly the "backfill from custody" behavior the user asked for. |

Each leaf's own `SKILL.md` is **the** authority for its inputs, outputs, and
guardrails. This orchestrator must not duplicate that logic — it decides
scope, aggregates results across leaves, and captures each leaf's structured
output verbatim (leaf-output-opacity rule shared with the daily routines).

### Reciprocity convention with `transaction-workday-audit` — no loop

`account-transaction:transaction-workday-audit` Check 1 is an **executing**
orchestrator: it detects the four onboarding gaps and, when any is non-zero,
invokes THIS skill (`new-asset-onboarding`) exactly once to execute the fix.
So the call graph is:

```
transaction-workday-audit Check 1  ──invokes──▶  new-asset-onboarding
       ▲                                                 │
       │           (must NOT re-invoke ─ would loop)     ▼
       └─────────────────────────  reads Check 1's detector SQL directly
```

**Rule for this skill.** When gathering Step 1's detection input, this skill
**executes the SQL** documented in `transaction-workday-audit` Check 1's
sub-detectors 1a / 1b / 1c / 1d directly via `mcp__ayunit__execute_select_query`.
It does NOT invoke `transaction-workday-audit` via the `Skill` tool — that
would create the cycle `Check 1 → new-asset-onboarding → transaction-workday-audit
→ Check 1`. The audit is the source of truth for those queries; this skill
copies them (or points at them) for its detection phase only.

If the queries in `transaction-workday-audit` Check 1 change, propagate the
change here in the same commit (see the doc-coherence rule in the repo-level
CLAUDE.md).

## Inputs

| Input | Default | Notes |
|---|---|---|
| `window_days` | `3` | Sweep back N days from `end_date`. Any int ≥ 1. |
| `end_date` | `today` (server date) | Explicit end-date is honored **verbatim**. Do NOT silently extend to yesterday / last business day — the caller's date scopes both the audit query and the `asset-price-history` window. |
| `custody_filter` | `[]` (= ALL custodies) | Optional list of custody names (`"BTG"`, `"XP"`, `"UBS Miami"`, `"JPM"`, `"MS"`, …). Empty = all. Applied to every detector query as `AND t.Custody IN (<custody_filter>)`. |
| `account_filter` | `[]` (= ALL accounts within the custody scope) | Optional list of `ClientAccount` codes (zero-padded per the custody convention — `"005707901"` on BTG, `"94317"` on XP onshore, etc.). Empty = all. Non-empty **switches the routine to ad-hoc mode**: applied to every detector query as `AND t.ClientAccount IN (<account_filter>)`, `AND cp.Account IN (<account_filter>)` on 1c, and — because the semantics of an ad-hoc surgical run differ from a fleet-wide daily sweep — the routine **skips the state lock** (no idempotency file, no short-circuit on today's lock) and writes the report under `reports/ad-hoc/`. Meant for the "audit surfaced N gaps on account X, fix them" workflow initiated from `transaction-workday-audit` Check 1's executing invocation. |
| `dry_run` | `false` | `true` → every leaf skill is also dry-run / read-only; no `AssetCustody` INSERTs, no `Global.Asset` INSERTs, no `AssetData.Price` INSERTs, no `pending-revalidate` writes; no state lock; report written under `reports/dry-run/`. |
| `force` | `false` | `true` → ignore today's state lock and run anyway. Use only on a confirmed crash mid-way. Ignored when `account_filter` is non-empty (ad-hoc mode has no lock to bypass). |

**Echo the resolved `(window_days, end_date, custody_filter, account_filter,
dry_run, force)` at the start of every reply.** If `dry_run = true`, prefix
every status line with `[DRY-RUN]`; if `account_filter` is non-empty, prefix
every status line with `[AD-HOC]` and include the account list. Reply in
the analyst's language (PT or EN); JSON field names always English.

## State / idempotency

State and report locations (try in order; the first that exists wins,
otherwise create the OneDrive-synced one for a Sten analyst):

1. `%USERPROFILE%/OneDrive - STEN/Documents/sten-routines/` (preferred)
2. `%USERPROFILE%/Documents/sten-routines/` (fallback)

Subfolders: `state/`, `reports/`. Files:

- `state/<end_date>_new_asset_onboarding.lock` — presence means the routine
  completed for that end-date. Content: the absolute path of the markdown
  report.
- `reports/<end_date>_new_asset_onboarding.json` and `.md` — the run's
  report.

Before doing anything:

- **Ad-hoc mode** (`account_filter` non-empty): skip the state lock entirely
  (no read, no short-circuit, no write); write the report under
  `reports/ad-hoc/<end_date>_<account_list>_new_asset_onboarding.{json,md}`
  where `<account_list>` is the joined list (e.g. `005707901` or
  `005707901+94317`). Multiple ad-hoc runs per day for the same account list
  are allowed — the report filename disambiguates.
- If today's lock exists **and** `force = false` **and** ad-hoc mode is off:
  short-circuit. Read the previous report and reply *"Already ran
  new-asset-onboarding for `<end_date>` — here's the report"* followed by the
  **Human action required** section from that report. **Do not** invoke any
  leaf skill.
- If `dry_run = true`, never read or write the state lock; write reports
  under `reports/dry-run/`.
- The lock is written only at the end of a successful **fleet-wide** real
  run (§7). Ad-hoc runs never write a lock.

## Reference resources (read on demand)

| Resource | Read when… |
|---|---|
| [`ayunit://docs/transaction/faq`](ayunit://docs/transaction/faq) | Refresh why `PENDING` with `Asset IS NULL` never reaches `AccountPosition` — the reason this routine exists. |
| [`ayunit://docs/transaction/procedure`](ayunit://docs/transaction/procedure) | `AccountTransaction_Update` params + the `U` overwrite semantic that `pending-revalidate` relies on. |
| [`ayunit://docs/portfolio-creator/pipeline`](ayunit://docs/portfolio-creator/pipeline) | Why the orchestrator does NOT trigger a PortfolioCreator recompute — the custody routines that run after this one own that step. Unblocking PENDINGs alone changes no `AccountPosition` state until the pipeline runs. |
| [`ayunit://docs/checkeddate/usage`](ayunit://docs/checkeddate/usage) | The lock every leaf writer respects. This skill never advances it. |

## Tools this skill calls directly

- `mcp__ayunit__execute_select_query` — Step 1 detection (executes the four
  sub-detector SQL queries documented in `account-transaction:transaction-workday-audit`
  Check 1 — sub-detectors 1a / 1b / 1c / 1d) AND Step 6 verify (re-runs the
  same four queries). Per the reciprocity convention above, this skill reads
  the audit's SQL and executes it directly rather than invoking the audit via
  the `Skill` tool.
- `mcp__ayunit__upload_blob_file` — §7 finalisation. Upload both the `.json`
  and the `.md` report to the ayunit blob store.
- `Skill` — invoke each leaf skill in Steps 2–5 (`asset:asset-lookup`,
  `asset:register-br-funds`, `asset:asset-register`, `asset:assetcustody-fill`,
  `account-transaction:pending-revalidate`, `asset:asset-price-history`).
- **No** `execute_procedure`, **no** `execute_batch(dry_run=false)` from the
  orchestrator. Every write goes through a leaf skill.
- **Never** `Skill(account-transaction:transaction-workday-audit)` — see the
  "Reciprocity convention" above.

## The routine

### 1 — Pre-flight (always; refuse to proceed on any failure)

1. **MCP reachable.** Run `execute_select_query('SELECT 1')`. If it fails,
   abort — do not call any leaf skill without the MCP connected.
2. **State lock.** Apply the §state rules above.
3. **Skeleton report.** Initialise the in-memory report shape from
   `references/step-schemas.md` (`run_meta.started_at = <now>`, every step
   `status = "not_run"`).

### 2 — Step 1: detect (execute audit Check 1 SQL directly)

Execute the four sub-detector queries documented in
`account-transaction:transaction-workday-audit` Check 1 via
`mcp__ayunit__execute_select_query`, using the resolved scope in each query's
`<period>` placeholder:

- Period: `Date >= DATEADD(day, -<window_days>, '<end_date>') AND Date <= '<end_date>'`
- Custody filter (optional): apply `AND t.Custody IN (<custody_filter>)`
  when `custody_filter` is non-empty.
- Account filter (optional, ad-hoc mode): when `account_filter` is non-empty,
  apply `AND t.ClientAccount IN (<account_filter>)` to detectors 1a / 1b / 1d
  and `AND cp.Account IN (<account_filter>)` to detector 1c (which reads
  `Portfolio.v_CustodyPosition`). The audit's Check 1c is already scoped to
  the audited `(Account, Custody)` pairs on the tape, so the same filter
  applies naturally.

The four detectors and what each surfaces:

| Sub-detector | What it returns | Onboarding stage the fix addresses |
|---|---|---|
| **1a** — `Global.Asset` missing (identifier resolves nowhere) | Tuples `{Custody, AssetCustody, CustodyIdentifier, FirstSeen, LastSeen, RowCount, Accounts, SamplePk}` where the tape's `Asset IS NULL` and NO `Global.v_Asset` row matches on any identifier column | Needs full four stages: register → map → position back-fill → price back-fill. |
| **1b** — `Portfolio.AssetCustody` mapping missing (Global.Asset EXISTS) | Same tuple shape as 1a **plus** `{ResolvedAsset, ResolvedCount}` — the audit has already resolved which `Global.Asset` code the identifier points to (via `Cnpj`/`Isin`/`BbgCode`/etc. probe) | Needs three stages: map → position back-fill → price back-fill. **Skips** register — Global.Asset row already exists. Ambiguity guard: `ResolvedCount > 1` = multiple candidates → do NOT auto-map, flag for analyst. |
| **1c** — `Portfolio.CustodyPosition` still `Asset=NULL AssetR=<code>` on audited (Account, Custody, period) scope | `{Custody, Account, AssetR, IsinR, AnbimaCodeR, FirstSeen, LastSeen, RowCount, TotalQuantity, TotalValue}` — rows the loader ingested before the mapping existed and never retroactively resolved | Needs position back-fill only (`assetcustody-fill`'s `CustodyPosition_Update @CMD='Update_Missing_Asset'` phase). Often overlaps with 1b — the same `assetcustody-fill` invocation clears both. |
| **1d** — `AssetData.Price` backfill gap from earliest trade date to today | Per-asset `{Asset, Description, AssetGroup, Currency, EarliestTapeDate, LatestTapeDate, EarliestPriceDate, LatestPriceDate, PriceDatesCovered, ApproxBusinessDaysExpected, ApproxGapCount, tier}` for registered+mapped assets with gaps (`PriceDatesCovered` uses `COUNT(DISTINCT p.Date)` so multi-source assets aren't inflated; `ApproxGapCount` is clamped at 0) | Needs price back-fill only (`asset-price-history` on `[EarliestTapeDate, today]` per asset). |
| Sidebar — Asset FK broken (rare corruption) | `{Asset, Custody, FirstSeen, LastSeen, RowCount, SamplePk}` — tape rows with a non-null `Asset` code that doesn't exist in `Global.v_Asset` | Not a routine gap; flag for analyst (data corruption or a hand-deleted row). |

Capture the result as JSON matching `step1_detect` in
`references/step-schemas.md` — four lists (`stage_1a_needs_registration`,
`stage_1b_needs_mapping_only`, `stage_1c_needs_position_backfill`,
`stage_1d_needs_price_backfill`) plus `asset_fk_broken` (sidebar) and
`pending_pks_at_risk` (the aggregated pks from 1a + 1b for later use in
`pending-revalidate` scope).

If **all four sub-detector lists AND the sidebar are empty** and `errors` is
empty → record every downstream step as `{status: "skipped", reason:
"nothing to onboard"}` and jump to §7 (verify) then §8 (report).

### 3 — Step 2: classify (asset-lookup safety pre-check on 1a only)

Sub-detectors 1b / 1c / 1d are **pre-classified by the audit itself** —
`asset-lookup` is NOT needed for them:

- **1b tuples** carry `ResolvedAsset` already (the audit resolved the
  Global.Asset code via its identifier probe). Route directly to Step 4 as
  `stage_1b_ready_for_mapping`. If a tuple has `ResolvedCount > 1`
  (ambiguous mapping target), route it to `ambiguous_assets` instead — do
  NOT let `assetcustody-fill` auto-pick.
- **1c rows** are `CustodyPosition` rows needing the `Update_Missing_Asset`
  back-fill. Route them to Step 4 as `stage_1c_ready_for_backfill`. If any
  1c row's custody also has a 1b tuple, the same `assetcustody-fill`
  invocation clears both; if a 1c custody has no 1a/1b, `assetcustody-fill`
  is still invoked in back-fill-only mode.
- **1d assets** are registered + mapped; only prices are missing. Route
  them directly to Step 6 as `stage_1d_ready_for_prices` — skip Steps 2, 3,
  4, 5.

For **1a tuples only**, invoke `asset:asset-lookup` with `(Custody,
TickerCustody = AssetCustody or CustodyIdentifier)` as identifier hints. Do
NOT batch — call per tuple. Route each result:

| Bucket | Condition | Next step |
|---|---|---|
| `known_by_lookup` | Leaf returns `verdict = FOUND` on Global.Asset or v_AssetCustody | Audit's identifier probe missed a match asset-lookup catches (rare false-negative). Skip register; feed to Step 4 alongside 1b tuples for the mapping insert. |
| `br_fund_candidate` | Leaf returns `NOT_FOUND` **and** the identifier looks like a CNPJ (14 digits after stripping punctuation, or the raw string matches `xx.xxx.xxx/xxxx-xx`) | Register path 3a. |
| `other_unknown` | Leaf returns `NOT_FOUND` and it's not CNPJ-shaped | Register path 3b (asset-register with peer analogy). |

**Sidebar (Asset FK broken) rows** go straight to `ambiguous_assets` with
`refusal_reason = "asset_fk_broken_corruption"` — do NOT try to auto-fix;
this is corruption, not a routine gap.

Capture as JSON matching `step2_classify`. Each bucket lists the resolved
tuples and their sample `pk`.

### 4 — Step 3: register (autonomous — BR funds + peer analogy)

For each tuple in `br_fund_candidate`, invoke the chain:

```
asset:register-br-funds  (CNPJ → ANBIMA lookup → pre-filled payload)
      ↓
asset:asset-register     (duplicate-gate → peer classification → preview → INSERT)
```

Pass `dry_run` through. Capture the returned Asset code (or the leaf's
`refused` verdict with reason).

For each tuple in `other_unknown`, invoke `asset:asset-register` directly
with:

- `AssetCustody` / `CustodyIdentifier` as identifier hints,
- a hint at the instrument kind if the tuple's `GeneralLedgerDescription`
  suggests one (e.g. contains `NTN-B` → treasury; `CDB` → bond; `PETR4` →
  equity; a 6-char ticker ending in `11` → FII),
- `peer_analogy = true` (the leaf's default classification strategy — copy
  FK conventions from existing assets of the same `SecurityType`).

If `asset-register` **refuses** (no confident peer set, required FK cannot
be resolved, ambiguous SecurityType) → capture the refusal reason and add
the tuple to `ambiguous_assets`. Do NOT retry with looser peers — the leaf
has already made the safe call.

Capture as JSON matching `step3_register` per the schema:

- `br_funds_registered`: list of `{Asset, Cnpj, source: "register-br-funds"}`
- `other_registered`: list of `{Asset, identifier, security_type, peer_sample: [Asset...], source: "asset-register"}`
- `ambiguous_assets`: list of `{Custody, AssetCustody, CustodyIdentifier, refusal_reason, SamplePk, note_for_analyst}` — flagged for the Human-action section AND the Azure Blob log.
- `errors`: list

### 5 — Step 4 + 5a: map + unblock PENDING

Aggregate the mapping-ready set from Steps 2 and 3:

- `known_by_lookup` (from Step 2 asset-lookup false-negative catches)
- `br_funds_registered + other_registered` (from Step 3 registration)
- `stage_1b_ready_for_mapping` (audit pre-classified: Global.Asset already
  exists, only the per-custody translation row is missing)

Build a single list of `(Asset, Custody, TickerCustody)` tuples and invoke
`asset:assetcustody-fill` once. Pass `dry_run` through. The leaf also runs
`Portfolio.CustodyPosition_Update @CMD='Update_Missing_Asset'` custody-wide,
which back-fills any `stage_1c_ready_for_backfill` rows on those custodies as
a byproduct (they share `fk_CustodyID`).

For any custody with `stage_1c_ready_for_backfill` rows but NO 1a/1b tuples
(pure 1c — mapping already exists but back-fill never ran), invoke
`asset:assetcustody-fill` in back-fill-only mode for that custody (no new
mapping insert; only `Update_Missing_Asset`).

Capture:

- `mappings_inserted`: count + list of new `AssetCustody` rows
- `custody_position_backfilled`: count of `Portfolio.CustodyPosition` rows
  updated by `Update_Missing_Asset` (across all invocations)
- `unblocked_pks`: aggregated list of pks that now have their custody
  identifier resolvable to a canonical Asset code (from 1a fixes + 1b fixes)
- `errors`: list

Then, if `unblocked_pks` is non-empty and `dry_run = false`, invoke
`account-transaction:pending-revalidate` scoped to those pks. The leaf's
auto-Asset-match validator will promote them PENDING → VALIDATED. Capture:

- `promoted_to_validated`: count
- `still_pending`: list of `{pk, reason}` (the mapping fired but another
  blocker — usually Price or Quantity — remained; NOT an ambiguous asset,
  just a downstream defect for the daily routine to pick up)
- `errors`: list

Capture the merged result into `step4_map_unblock` per the schema.

### 6 — Step 5b: historical prices

Assemble the price-backfill target set from three sources:

1. **Newly-registered assets** (`step3_register.br_funds_registered ∪
   step3_register.other_registered`) — freshly-registered 1a assets. Window
   per asset: `start_date = MIN(t.Date)` from the Step 1 audit's 1a rows
   whose identifier resolved to this Asset; `end_date` = the caller's
   `end_date`.
2. **Newly-mapped assets** (`stage_1b_ready_for_mapping` post-Step-4
   success) — already-registered assets that got their per-custody mapping
   this run. Skip if the asset's price coverage is already complete for the
   window (probe via re-running the 1d detector for this asset only). Else,
   same window rule as (1) using the audit's 1b rows for this asset.
3. **Pure 1d gaps** (`stage_1d_ready_for_prices` from Step 2) — assets
   registered + mapped in prior runs but with `AssetData.Price` gaps
   detected by 1d. Window per asset comes directly from the 1d row:
   `[EarliestTapeDate, today]`.

For each asset in the assembled set:

1. Invoke `asset:asset-price-history` with `(Asset, start_date, end_date,
   dry_run)`. The leaf walks its own 4-source priority chain (MarketData →
   AssetDataDB.v_Price → `Portfolio.v_CustodyPosition × PriceFactor`). Do
   NOT pre-filter sources here; that's the leaf's contract.
2. Capture per-Asset: `source_tag` (`newly_registered` / `newly_mapped` /
   `preexisting_1d_gap`), `dates_inserted`, `dates_still_missing`,
   `source_mix` (`{MarketData: n, AssetDataDB: n, CustodyPosition: n}`),
   `errors`.

Assets in `known_by_lookup` (Step 2 false-negatives) are already registered
AND mapped elsewhere — check whether they show up in the re-detected 1d
alongside sources (1) and (2); include only if they do.

Capture as `step5_price_history` per the schema, keyed by Asset code.

### 7 — Step 6: verify (read-only)

Re-execute every sub-detector SQL from `transaction-workday-audit` Check 1
(1a, 1b, 1c, 1d, and the sidebar) for the same window and custody filter.
Compare against Step 1:

- `resolved_1a`, `resolved_1b`, `resolved_1c`, `resolved_1d`: tuples /
  rows / assets that were in Step 1 but no longer appear (per sub-detector).
- `residual_expected`: rows / tuples that still appear AND are traceable to
  `step3_register.ambiguous_assets` OR `stage_1b_ready_for_mapping` items
  the orchestrator flagged with `ResolvedCount > 1` (auto-mapping declined
  by design).
- `residual_unexpected`: anything else that still appears → flag prominently
  (indicates a leaf write silently failed or the auto-Asset-match didn't
  fire despite the mapping insert).
- `regressed`: rows that didn't exist in Step 1 but appear now (worst
  case; almost never fires, but a signal that a leaf broke something).

Also count residual `PENDING` with `Asset IS NULL` on the same window as a
final sanity check.

Capture as `step6_verify` per the schema.

### 8 — Step 7: report + upload to Azure Blob

Two files (one if `dry_run = true` — only the markdown):

- `reports/<end_date>_new_asset_onboarding.json` — the full structured
  report per the schemas in `references/step-schemas.md`.
- `reports/<end_date>_new_asset_onboarding.md` — human-readable summary.
  **Lead with the Human action required section**, then per-step tables.

Write both with the `Write` tool. If a filesystem write tool is unavailable
in the current session, print the full report to chat and tell the user
*"the orchestrator could not write the report to disk — copy the content
above into `<path>`"*. Never silently drop the report.

Then, **for both files** (skip in `dry_run`), upload to the ayunit Azure
Blob store:

```
mcp__ayunit__upload_blob_file(
    file_path       = "<absolute path to the report file>",
    blob_name       = "routines/new-asset-onboarding/<end_date>_new_asset_onboarding.<ext>",
    container_name  = "<default — do not override unless the caller passed one>",
    overwrite       = false          # never overwrite silently; the state lock is our idempotency guard
)
```

The blob name is a fixed convention so downstream tooling (dashboards,
future Slack integration) can find the latest run for a given date without
guessing. If `overwrite=false` fails because a blob already exists at that
name, treat it as a benign duplicate (the orchestrator ran twice for the
same `end_date` — likely with `force=true`), log a warning to
`run_meta.errors`, and **do not retry with overwrite=true from here**. The
analyst decides.

Advance the state lock (unless `dry_run = true`).

### 9 — Finalise

Reply in chat with:

1. one-line status summary (`OK ✅ · N registered` / `⚠️ N ambiguous` /
   `❌ N failed`)
2. the report path(s), local + blob URL if returned by `upload_blob_file`
3. the count of items in **Human action required**
4. the resolved scope echo

## Critical rules

- **Writes are strictly leaf-owned.** The orchestrator issues zero
  `execute_procedure` / `execute_batch(dry_run=false)`. If a step requires
  a write, delegate to a leaf skill.
- **Act, don't ask** — for every asset the register path can classify with
  confidence (BR CNPJ or non-fund with a confident peer set). Only pause
  for human review when a leaf refuses. Report ambiguous cases in the
  final report and the blob log — do not interrupt the run to request
  approval mid-flight.
- **Never advance `CheckedDate`.** The lock is the safety net; advancing it
  is owned by the `checkeddate-update` / `execute_checked_date` specialist
  path.
- **Never trigger a PortfolioCreator recompute** — that is the daily
  custody routine's job. This routine only fixes the master-data side and
  unblocks PENDINGs; positions rebuild on the next `calculate_portfolio`
  the custody routine (or the analyst) runs.
- **Honor the caller's `end_date`** verbatim for both the audit window
  and the `asset-price-history` window's upper bound. Do not silently
  extend / clamp.
- **Dry-run is contagious.** If the orchestrator runs in `dry_run = true`,
  every leaf-skill invocation must also be in dry-run, no writes go to
  disk state, no upload to Azure Blob.
- **Continue-on-failure per bucket.** If `asset-register` refuses on one
  tuple, capture the refusal and continue with the next tuple. Never
  abort the whole run for a single failure.
- **Echo scope on every reply.** `(window_days, end_date, custody_filter,
  dry_run, force)`.
- **Reply language matches the analyst.** PT or EN. JSON field names
  always English; markdown report headings can be PT for Sten readers.
- **Never reach back into a leaf skill's internals.** If a leaf's output
  shape doesn't match the schema, report *"unexpected shape from leaf X"*
  and ask the user to re-align `references/step-schemas.md`.

## When unsure

- **All four sub-detectors (1a/1b/1c/1d) return 0.** Say so explicitly.
  Write a "nothing to onboard" report, upload to blob, finalise. This is a
  valid outcome and the most common one on days when custody loaders behave
  and no price gaps have accumulated.
- **The sidebar (Asset FK broken) returns > 0 rows.** This means an
  `AccountTransaction` row has an `Asset` code that doesn't exist in
  `Global.Asset` — data corruption, not a routine gap. Do NOT try to fix
  it. Flag as `ambiguous_assets` with `refusal_reason =
  "asset_fk_broken_corruption"` and point the analyst at the row.
- **Sub-detector 1b returns tuples with `ResolvedCount > 1`.** The identifier
  matches more than one `Global.Asset` row (e.g. a CNPJ shared by two
  registered class codes, an ISIN reused). Do NOT let `assetcustody-fill`
  auto-pick a target — flag as `ambiguous_assets` and let the analyst
  choose.
- **`asset-lookup` finds a 1a identifier in `Global.Asset` but not in
  `Portfolio.v_AssetCustody`.** That's `known_by_lookup` (audit false-
  negative caught by asset-lookup's broader probe) — skip register, hand
  straight to `assetcustody-fill` alongside 1b tuples. Efficient case.
- **`asset-register` refuses on a non-BR-fund unknown** (no confident
  peer set). Do NOT retry with a looser peer strategy. Log the tuple with
  the leaf's refusal reason and one-sentence guidance for the analyst
  (which fields need manual input). Continue.
- **`assetcustody-fill` returns `unblocked_pks = []` despite the mapping
  insert.** Either the mapping was already there (idempotent), or the
  audit false-flagged a tuple. Capture and continue — no residual action
  needed.
- **`pending-revalidate` reports `still_pending` after promotion.** These
  are downstream blockers (missing Price, missing Quantity) that the
  daily custody routine will address. NOT an ambiguous asset — do not
  add to `ambiguous_assets`.
- **`asset-price-history` finds zero source hits for a date range.** The
  asset was traded but no internal price source has any prices in that
  window. Capture `dates_still_missing` for the report; the analyst
  decides whether to trigger the Bloomberg enrichment skill
  (`asset-enrich-from-bbg`) — out of scope here.
- **`upload_blob_file` fails.** Do NOT abort the run — the disk report is
  the authoritative artifact. Capture the upload error in
  `run_meta.errors` and finalise.
- **Two `Activated=1` rows in `Portfolio.v_CheckedDate` for the same
  `(Account, Custody)`.** The scalar subquery inside
  `AccountTransaction_Update` raises (error 512) — `pending-revalidate`
  will fail on every promotion for that account. Do not fix from here.
  Mark the affected pks as `failed` with reason `broken_lock` and
  include the diagnostic query in the Human-action section.
- **The user asks to also run the daily custody routines as part of this
  run.** Refuse politely and point at `daily-btg-onshore-routine` /
  `daily-btg-offshore-routine`. Chaining them would collapse the state-
  lock discipline and interleave two independent audit passes.
