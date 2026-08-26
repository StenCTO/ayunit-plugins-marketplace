# Design — `unused-asset-audit` skill (asset plugin)

**Date:** 2026-08-25
**Plugin:** `asset` (bump `0.5.1 → 0.6.0`, minor per new skill)
**Author:** Pedro Trevisan

## 1. Purpose

Audit `Global.v_Asset` for `Activated=1` assets that have **zero footprint in both** `Portfolio.v_AccountPosition` and `Portfolio.v_CustodyPosition` across a lookback window (default 45 days ending today), then walk the user through per-row `Activated=0` flips via `Global.Asset_Update @CMD='U'`.

The goal is to stop wasting external-provider requests (Bloomberg, ANBIMA, custody feeds) on instruments the book no longer holds — the downstream loaders / price routines filter on `Activated=1`, so flipping the flag is the leverage point.

Read-heavy, write-scoped. Every write is one explicit yes away, per the plugin's shared design contract.

## 2. Triggers (PT/EN)

The skill's `description` frontmatter must list phrases like:

- "quais ativos ativos não temos mais posição"
- "faz uma varredura de ativos não utilizados"
- "estamos puxando cotação de fundo que não existe mais na carteira"
- "sweep unheld assets"
- "find assets we can deactivate"
- "clean up Global.Asset"
- "audit unused assets"

## 3. Inputs

| Input | Default | Notes |
|---|---|---|
| `lookback_days` | 45 | Positive integer. Window is `[today − lookback_days, today]`. |
| `end_date` | today (2026-08-25 at build time; runtime = current date) | ISO date. Rarely overridden; supports back-dated audits. |
| `include_fuzzy_categories` | user checklist per run | See §5 Layer 2. |

## 4. Non-goals

Explicit to keep scope tight:

- **No re-activation path.** Flipping `Activated=0 → 1` is a different judgment call and lives in a different skill if needed.
- **No `Portfolio.AssetCustody` mutation.** Per-custody mapping rows are read-only inputs to this skill; deactivating an Asset does not require deleting/deactivating its custody translations. If a mapping later needs cleanup, that's `assetcustody-fill` territory.
- **No external-provider unsubscription.** This skill only flips the DB flag; downstream loaders/schedulers stop querying automatically because they filter on `Activated=1`.
- **No batch write.** Per-row confirmation model (see §7). `execute_batch` is not used — the point is human eyeballs per row, and batches obscure that.

## 5. Exclusion filter (two-layer)

### Layer 1 — hardcoded structural floor (never candidates)

Grounded in the live `Global.v_Asset` taxonomy pulled 2026-08-25 (139 combos across `AssetGroup × SecurityType × Product × AssetClass`). The following categories are structural markers, never held-as-positions in the client sense:

```sql
AssetGroup NOT IN (
  'Benchmark',        -- 19 rows: indices / risk-free proxies
  'Cash',             -- 13 rows: cash equivalents (BRL, USD, EUR…)
  'Settlement',       -- 4 rows: unsettled trade markers
  'Compromissada',    -- 2 rows: repo lines
  'CPR',              -- 4 rows: provisioned receivables
  'Future',           -- 2 rows: generic futures
  'Future Currency',  -- 1 row
  'Futuro DAP',       -- 1 row
  'Futuro DI',        -- 2 rows
  'FX'                -- 19 rows: NDFs / FX hedges
)
AND AssetClass NOT IN (
  'FX Hedge',
  'Provisão',
  'Benchmark'
)
```

Belt-and-suspenders: the `AssetClass` clause catches any row that slips through `AssetGroup` (e.g. `AssetGroup='Equity', AssetClass='Benchmark'` — 1 row exists).

### Layer 2 — live checklist (fuzzy categories, per-run)

The skill queries the DB for the distinct classification combos that survive Layer 1, then prompts the user:

> "Include these categories in the sweep? (default: all ticked)
> [x] Hedge Fund (`AssetClass='Hedge Fund'` — 127 assets)
> [x] Private Equity / Private Credit / Real Estate / Special Situations (`Product IN ('Alternative','Alternativos')`  — 165 assets)
> [x] Ilíquidos Exterior (`AssetClass='Ilíquidos Exterior'` — 15 assets)
> [x] COE (`AssetGroup='COE'` — 37 assets)"

Unchecking a category excludes it from that run only — no persistence, no config file.

**Rationale for two layers:** benchmarks, cash, settlement, compromissada, CPR, futures, and FX hedges are structural — flagging them is always a false positive, so hardcoded exclusion saves the user a question. Illiquid alternatives and COE are judgment calls that depend on which funds are actively watched — a live checklist keeps the user in control without editing the skill.

### Liquid target set (after both layers)

Covers: `Bond`, `Equity` (Stock/ETF), `Tesouro`, `Treasury`, `LCI/LCA`, `Debênture`, `CRI/CRA`, `RF Não Listada` (CDBs, LFs), `FII`, most `Fundos`, `Mutual Fund`, `Time Deposit`, `Option`, `CBIO`, `CLN`.

## 6. Detection query

Single scan, everything server-side. Column names verified at skill runtime via `get_table_detail` (per the plugin "no hard-coded schema" contract):

```sql
DECLARE @end   date = CAST(GETDATE() AS date);
DECLARE @start date = DATEADD(day, -@lookback_days, @end);

WITH held_account AS (
  SELECT DISTINCT fk_AssetID
  FROM Portfolio.v_AccountPosition
  WHERE Date BETWEEN @start AND @end AND ABS(Quantity) > 0
),
held_custody AS (
  SELECT DISTINCT fk_AssetID
  FROM Portfolio.v_CustodyPosition
  WHERE Date BETWEEN @start AND @end AND ABS(Quantity) > 0
),
last_seen AS (
  SELECT fk_AssetID, MAX(Date) AS LastHeldDate
  FROM (
    SELECT fk_AssetID, Date FROM Portfolio.v_AccountPosition
      WHERE Date < @start AND ABS(Quantity) > 0
    UNION ALL
    SELECT fk_AssetID, Date FROM Portfolio.v_CustodyPosition
      WHERE Date < @start AND ABS(Quantity) > 0
  ) x
  GROUP BY fk_AssetID
),
accts_hist AS (
  SELECT fk_AssetID, COUNT(DISTINCT Account) AS AccountsHistorical
  FROM Portfolio.v_AccountPosition
  WHERE ABS(Quantity) > 0
  GROUP BY fk_AssetID
)
SELECT
  a.Asset,
  a.AssetGroup,
  a.SecurityType,
  a.AssetClass,
  a.Currency,
  ls.LastHeldDate,
  COALESCE(ah.AccountsHistorical, 0) AS AccountsHistorical,
  a.RegisteredOn                              -- verified column name at runtime
FROM Global.v_Asset a
LEFT JOIN last_seen  ls ON ls.fk_AssetID = a.pk_AssetID
LEFT JOIN accts_hist ah ON ah.fk_AssetID = a.pk_AssetID
WHERE a.Activated = 1
  AND a.pk_AssetID NOT IN (SELECT fk_AssetID FROM held_account)
  AND a.pk_AssetID NOT IN (SELECT fk_AssetID FROM held_custody)
  AND a.AssetGroup  NOT IN (:LAYER1_ASSETGROUPS)
  AND a.AssetClass  NOT IN (:LAYER1_ASSETCLASSES)
  AND (:LAYER2_CATEGORY_FILTER)
ORDER BY a.AssetGroup, ls.LastHeldDate DESC;
```

**`LastHeldDate` semantics:** the most recent date the asset was ever held **before the window started** (`Date < @start`). This distinguishes "quiet since March, held for years" (recent activity but out of window) from "quiet forever" (registered but never held — possibly a bad registration).

**Runtime schema probe.** Before the scan, skill calls `get_table_detail` on `Global.v_Asset`, `Portfolio.v_AccountPosition`, `Portfolio.v_CustodyPosition` to confirm every column referenced exists. If a column is missing (e.g. `RegisteredOn` is actually `CreatedOn` or `InsertDate`), the skill picks the right one from the introspection result rather than failing.

## 7. Preview and per-row confirmation

Flat table shown to user (all rows, ordered by `AssetGroup, LastHeldDate DESC`):

```
#  | Asset       | AssetGroup   | SecurityType | AssetClass      | Ccy | LastHeld   | Accts | RegOn
1  | DEB1234ABC  | Debênture    | Debênture    | Crédito Privado | BRL | 2025-11-14 | 3     | 2023-02-01
2  | DEB5678XYZ  | Debênture    | Debênture    | Crédito Privado | BRL | 2025-09-02 | 1     | 2024-01-10
3  | STCKSAMPLE  | Equity       | Stock        | Single Stock    | BRL | 2025-06-30 | 5     | 2022-04-15
…
```

Then:

> "42 candidates found. Deactivate which? (comma-list of #, ranges OK e.g. `1,3,7-12`; `all` / `none`) → "

Parser accepts `all`, `none`, or a mixed list of `N` and `N-M` tokens. On any parse error, re-prompts (does not proceed on ambiguity).

Skill echoes the parsed selection back for a final yes/no:

> "Deactivate these 7 assets? DEB1234ABC, STCKSAMPLE, … (y/N) → "

Only on `y` does the write path start.

## 8. Write path

Per confirmed row (loop, not batch — the per-row confirmation model exists precisely to make each write reviewable):

1. **Fetch full row** — `SELECT * FROM Global.v_Asset WHERE Asset = @Asset` for every populated column. Per repo `CLAUDE.md` §0 golden rule: `U` overwrites every column from the params passed; omitted fields become `NULL`. This step is non-negotiable.
2. **Drop computed fields** from the SELECT result — any auto-computed / view-only columns the procedure rejects (analogous to how `AccountTransaction_Update` rejects `AccountCurrency`/`AccountFx`). Verified at skill build via `get_procedure_detail` on `Global.Asset_Update`.
3. **Build `Global.Asset_Update @CMD='U'`** with all fields carried forward + `Activated = 0` + `Obs` / `AgentCheck` documenting the sweep:
   - `Obs`: preserve existing + append ` | unused-asset-audit 2026-08-25: no AccountPosition/CustodyPosition rows in last 45d, LastHeld=<date>`
   - `AgentCheck`: `unused-asset-audit`
4. **Call `execute_procedure`** with those params.
5. **Verify** — `SELECT Activated FROM Global.v_Asset WHERE Asset = @Asset` returns `0`. If not, surface as error and stop the loop for user attention.
6. Print `[<Asset>] deactivated ✓` and continue to next row.

**Failure handling.** Deactivations are independent per asset; if row N fails, log the error inline, keep going. At the end, print a summary: "OK: X, FAILED: Y (see errors above)".

## 9. Files touched

- **New:** `plugins/asset/skills/unused-asset-audit/SKILL.md` — frontmatter (`name: unused-asset-audit`, `description:` with the §2 trigger phrases) + body implementing §§3–8.
- **Modified:** `plugins/asset/.claude-plugin/plugin.json` — bump `version: "0.5.1" → "0.6.0"` (minor, new skill). Update `description` to mention the new skill.
- **Modified:** `plugins/asset/README.md` — add a row to the skills table describing `unused-asset-audit`; update the footer version.
- **Modified:** repo root `README.md` — update the "Current contents" table row for the `asset` plugin (skill count / version).

**Not modified:** `.claude-plugin/marketplace.json` (no new plugin), no `.mcp.json`.

## 10. Versioning and release

Per repo `CLAUDE.md` §§0, 2, 3.5:

1. Branch from `main`: `git checkout -b unused-asset-audit`.
2. Implement §9 files.
3. **Doc-coherence grep** before commit:
   ```bash
   grep -rn "unused-asset-audit" --include="*.md" .
   grep -n "^| \`asset\` \|" README.md
   grep -n "asset v0\." plugins/asset/README.md
   ```
4. Commit style: `asset v0.6.0: add unused-asset-audit skill (Global.Asset Activated=1 sweep with two-layer filter and per-row Activated=0 flip)`.
5. Push to **both** remotes: `git push github unused-asset-audit && git push origin unused-asset-audit`.
6. Open PR on GitHub, merge (squash + delete branch) — this fires the marketplace CDN sync.
7. Local ff-merge `main`, then `git push origin main` to mirror.

## 11. Validation plan

Before declaring the skill done:

1. **Dry-run against production data.** Run the detection query with `lookback_days = 45`, no writes. Sanity-check: does the count look plausible (dozens, not thousands)? Spot-check 3–5 assets manually — are they actually stale?
2. **Layer 1 sanity.** Confirm zero benchmarks / cash / settlement / futures in the candidate list.
3. **Layer 2 UX check.** Verify the checklist renders and unchecking a category correctly excludes it.
4. **Write path smoke test.** Pick ONE candidate the user agrees is safe, run through the full flow end-to-end, verify `Activated=0` in `Global.v_Asset`, verify the audit trail in `Obs`.
5. **Idempotency check.** Re-running the skill immediately after should show the just-deactivated assets are gone from the candidate list (because `Activated=1` is the filter).

## 12. Open questions

None at spec time. Any surprises found during implementation (e.g., `Global.Asset_Update` rejects a column not anticipated here) are absorbed into the plan when writing-plans is invoked.
