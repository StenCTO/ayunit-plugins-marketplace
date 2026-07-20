---
name: new-asset-onboarding
description: "Use when the user wants to sweep recently-inserted Portfolio.AccountTransaction rows across ALL custodies for assets not yet in Global.Asset or not yet mapped in Portfolio.AssetCustody, and onboard them end-to-end — register the security in Global.Asset, add the per-custody translation row, bulk-backfill Portfolio.CustodyPosition, unblock PENDING trades, and backfill historical prices in AssetData.Price. This is an **orchestrator skill** (meta-skill): it does NOT write to the DB directly; it chains the leaf skills account-transaction:transaction-workday-audit (read-only detector), asset:asset-lookup (safety pre-check), asset:register-br-funds + asset:asset-register (registration paths), asset:assetcustody-fill (mapping + CustodyPosition backfill), account-transaction:pending-revalidate (promote unblocked PENDINGs), and asset:asset-price-history (price backfill). Runs autonomously — for Brazilian fund CNPJs the register-br-funds → asset-register chain; for other kinds (equities, options, bonds, offshore funds, FIIs, treasuries) invokes asset-register with peer-analogy classification (copies FK conventions — Issuer, AssetClass, Product, Benchmark, Source, TaxRegime — from existing assets of the same SecurityType). Ambiguous cases the orchestrator cannot safely classify are logged, not guessed. Reports go to disk AND are uploaded to Azure Blob via mcp__ayunit__upload_blob_file. Designed to run BEFORE the daily custody routines so they find zero unmapped assets. Fires on prompts like 'onboard new assets from this week', 'register any unknown assets in the last 3 days', 'sweep unmapped assets', 'run the new-asset onboarding routine', 'roda o new-asset-onboarding para os últimos 3 dias', 'cadastra os ativos novos que apareceram nos trades recentes'."
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
| 1 | `account-transaction:transaction-workday-audit` | Read-only detector. Check 1a surfaces unmapped `(Custody, AssetCustody, CustodyIdentifier)` tuples on rows with `Asset IS NULL AND ac.Asset IS NULL`. Check 1b surfaces rows where `Asset` is set but not in `Global.Asset` (rare/corruption). |
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

## Inputs

| Input | Default | Notes |
|---|---|---|
| `window_days` | `3` | Sweep back N days from `end_date`. Any int ≥ 1. |
| `end_date` | `today` (server date) | Explicit end-date is honored **verbatim**. Do NOT silently extend to yesterday / last business day — the caller's date scopes both the audit query and the `asset-price-history` window. |
| `custody_filter` | `[]` (= ALL custodies) | Optional list of custody names (`"BTG"`, `"XP"`, `"UBS Miami"`, `"JPM"`, `"MS"`, …). Empty = all. Passed through to `transaction-workday-audit`. |
| `dry_run` | `false` | `true` → every leaf skill is also dry-run / read-only; no `AssetCustody` INSERTs, no `Global.Asset` INSERTs, no `AssetData.Price` INSERTs, no `pending-revalidate` writes; no state lock; report written under `reports/dry-run/`. |
| `force` | `false` | `true` → ignore today's state lock and run anyway. Use only on a confirmed crash mid-way. |

**Echo the resolved `(window_days, end_date, custody_filter, dry_run,
force)` at the start of every reply.** If `dry_run = true`, prefix every
status line with `[DRY-RUN]`. Reply in the analyst's language (PT or EN);
JSON field names always English.

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

- If today's lock exists **and** `force = false`: short-circuit. Read the
  previous report and reply *"Already ran new-asset-onboarding for
  `<end_date>` — here's the report"* followed by the **Human action
  required** section from that report. **Do not** invoke any leaf skill.
- If `dry_run = true`, never read or write the state lock; write reports
  under `reports/dry-run/`.
- The lock is written only at the end of a successful real run (§7).

## Reference resources (read on demand)

| Resource | Read when… |
|---|---|
| [`ayunit://docs/transaction/faq`](ayunit://docs/transaction/faq) | Refresh why `PENDING` with `Asset IS NULL` never reaches `AccountPosition` — the reason this routine exists. |
| [`ayunit://docs/transaction/procedure`](ayunit://docs/transaction/procedure) | `AccountTransaction_Update` params + the `U` overwrite semantic that `pending-revalidate` relies on. |
| [`ayunit://docs/portfolio-creator/pipeline`](ayunit://docs/portfolio-creator/pipeline) | Why the orchestrator does NOT trigger a PortfolioCreator recompute — the custody routines that run after this one own that step. Unblocking PENDINGs alone changes no `AccountPosition` state until the pipeline runs. |
| [`ayunit://docs/checkeddate/usage`](ayunit://docs/checkeddate/usage) | The lock every leaf writer respects. This skill never advances it. |

## Tools this skill calls directly

- `mcp__ayunit__execute_select_query` — only the verify SELECT in §6 (re-run
  of Check 1a on the same window). All other reads happen through the
  leaf skills.
- `mcp__ayunit__upload_blob_file` — §7 finalisation. Upload both the `.json`
  and the `.md` report to the ayunit blob store.
- **No** `execute_procedure`, **no** `execute_batch(dry_run=false)` from the
  orchestrator. Every write goes through a leaf skill.

## The routine

### 1 — Pre-flight (always; refuse to proceed on any failure)

1. **MCP reachable.** Run `execute_select_query('SELECT 1')`. If it fails,
   abort — do not call any leaf skill without the MCP connected.
2. **State lock.** Apply the §state rules above.
3. **Skeleton report.** Initialise the in-memory report shape from
   `references/step-schemas.md` (`run_meta.started_at = <now>`, every step
   `status = "not_run"`).

### 2 — Step 1: detect (transaction-workday-audit)

Invoke `account-transaction:transaction-workday-audit` with the resolved
scope:

- Period: `Date >= DATEADD(day, -<window_days>, '<end_date>') AND Date <= '<end_date>'`
- Custody filter: `custody_filter` (empty = all)
- Checks: **Check 1a (unmapped identifiers) + Check 1b (Asset not in
  Global.Asset)** only. Do NOT enable the duplicate check or the stuck-
  PENDING check — those are the custody routines' concern.

Capture the result as JSON matching `step1_detect` in
`references/step-schemas.md`:

- `check_1a_unmapped_tuples`: list of `{Custody, AssetCustody, CustodyIdentifier, FirstSeen, LastSeen, RowCount, Accounts, SamplePk}`
- `check_1b_asset_not_in_global`: list of `{pk, Asset, Custody, Date, GeneralLedgerDescription}` (should almost always be empty)
- `pending_pks_at_risk`: aggregated set of `pk_AccountTransactionID` from all rows contributing to Check 1a (needed later for `pending-revalidate` scope; the audit already returns them per tuple)
- `errors`: list

If **both check lists are empty** and `errors` is empty → record every
downstream step as `{status: "skipped", reason: "nothing to onboard"}` and
jump to §6 (verify) then §7 (report).

### 3 — Step 2: classify (asset-lookup safety pre-check)

For each distinct tuple returned by Check 1a, invoke `asset:asset-lookup`
with `(Custody, TickerCustody = AssetCustody or CustodyIdentifier)` as
identifier hints. Do NOT batch into one call; the leaf can be called per
tuple, and per-tuple output is what the report needs.

For each result, place the tuple into exactly one of three buckets:

| Bucket | Condition | Next step |
|---|---|---|
| `known_by_lookup` | Leaf returns `verdict = FOUND` on Global.Asset or v_AssetCustody | Skip register. Feed directly to Step 4 for the per-custody mapping insert (asset exists, only the AssetCustody row is missing). |
| `br_fund_candidate` | Leaf returns `NOT_FOUND` **and** the identifier looks like a CNPJ (14 digits after stripping punctuation, or the raw string matches `xx.xxx.xxx/xxxx-xx`) | Register path 3a. |
| `other_unknown` | Leaf returns `NOT_FOUND` and it's not CNPJ-shaped | Register path 3b (asset-register with peer analogy). |

For Check 1b tuples, always place them into `check_1b_recheck` — the row
already has an `Asset` code but that code doesn't exist in `Global.Asset`.
Do NOT try to auto-register; flag as `ambiguous_assets` (this is
corruption, not a routine gap) and let the analyst investigate.

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

Aggregate `known_by_lookup + br_funds_registered + other_registered` into a
single list of `(Asset, Custody, TickerCustody)` tuples and invoke
`asset:assetcustody-fill` once. Pass `dry_run` through. Capture:

- `mappings_inserted`: count + list of new `AssetCustody` rows
- `custody_position_backfilled`: count of `Portfolio.CustodyPosition` rows
  updated by `Update_Missing_Asset`
- `unblocked_pks`: aggregated list of pks that now have their custody
  identifier resolvable to a canonical Asset code
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

### 6 — Step 5b: historical prices (per registered asset)

For each Asset in `step3_register.br_funds_registered ∪
step3_register.other_registered`:

1. Determine the price window: `start_date = MIN(t.Date)` from the Step 1
   audit rows whose `(Custody, AssetCustody or CustodyIdentifier)` resolved
   to this Asset; `end_date` = the caller's `end_date`.
2. Invoke `asset:asset-price-history` with `(Asset, start_date, end_date,
   dry_run)`. The leaf walks its own 4-source priority chain (MarketData →
   AssetDataDB.v_Price → `Portfolio.v_CustodyPosition × PriceFactor`) — the
   CustodyPosition fallback is exactly the "backfill from custody" behavior
   the user asked for. Do NOT pre-filter sources here; that's the leaf's
   contract.
3. Capture per-Asset: `dates_inserted`, `dates_still_missing`, `source_mix`
   (`{MarketData: n, AssetDataDB: n, CustodyPosition: n}`), `errors`.

Skip this step entirely for assets in `known_by_lookup` — they were already
registered, so their historical prices are already someone else's problem
(they were populated when that asset was first onboarded).

Capture as `step5_price_history` per the schema, keyed by Asset code.

### 7 — Step 6: verify (read-only)

Re-run `transaction-workday-audit` Check 1a for the same window and
custody filter. Compare against Step 1:

- `resolved`: tuples in Step 1 that no longer appear.
- `residual`: tuples that still appear. **Expected residuals** =
  `ambiguous_assets` (the orchestrator did not register them by design).
  **Unexpected residuals** = anything else → flag prominently (indicates
  a leaf write silently failed or the auto-Asset-match didn't fire despite
  the mapping insert).
- `regressed`: tuples that didn't exist in Step 1 but appear now (worst
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

- **Check 1a returns 0 tuples.** Say so explicitly. Write a "nothing to
  onboard" report, upload to blob, finalise. This is a valid outcome and
  the most common one on days when custody loaders behave.
- **Check 1b returns >0 rows.** This means an `AccountTransaction` row has
  an `Asset` code that doesn't exist in `Global.Asset` — data corruption,
  not a routine gap. Do NOT try to fix it. Flag as `ambiguous_assets` and
  point the analyst at the row.
- **`asset-lookup` finds the identifier in `Global.Asset` but not in
  `Portfolio.v_AssetCustody`.** That's `known_by_lookup` — skip register,
  hand straight to `assetcustody-fill`. This is a mapping-only gap and
  the most efficient case.
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
