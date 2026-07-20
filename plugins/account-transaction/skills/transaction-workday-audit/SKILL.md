---
name: transaction-workday-audit
description: "Use when the user wants a daily / workday audit + fix cycle of Portfolio.AccountTransaction — an EXECUTING orchestrator that (a) surfaces the recurring defects an analyst resolves during their routine and (b) DRIVES the fix by invoking each responsible sibling skill in the right order. Scopes any period (default: last 7 days on Date), runs Check 1 (four-stage onboarding-pipeline detector that hands off to the `asset` plugin / `routines:new-asset-onboarding`) and Check 2 (six-step sequential fix orchestrator that invokes account-transaction siblings in order: compromissada-fix → assetrelated-fix → pending-revalidate → pending-position-repair → duplicate-trade-reconcile → position-quantity-adjustment), re-probes the tape between steps to confirm each bucket shrank, and reports totals at the end. WRITES happen via sibling skills (each with its own preview/confirm/execute cycle) — this skill does NOT write directly. The ONLY hard invariant is each account's CheckedDate: siblings enforce the lock on every write, and this skill never moves a CheckedDate. Trigger whenever the user says 'audit the tape', 'workday checks', 'daily checks', 'roda o fluxo do dia', 'o que precisa resolver hoje', 'quais ativos não estão cadastrados', 'processa os PENDING', 'roda a orquestração de conserto', or asks for a rundown + fix of unresolved items in AccountTransaction for a period."
---

# Workday audit — surface defects AND drive their fix

You are the executing orchestrator for the analyst's **routine audit + fix
cycle** on `Portfolio.AccountTransaction`. Every workday there is a recurring
set of defects that the loader / validators cannot solve on their own — and a
fixed sequence of sibling skills that resolve them: unregistered assets → the
`asset` plugin, unattributed income → `assetrelated-fix`, stuck `PENDING`
rows → `pending-revalidate`, duplicates → `duplicate-trade-reconcile`, and so
on. This skill is the **single entry point** that scans the recent tape,
detects each defect, **invokes the right sibling to fix it in the right
order**, and re-probes between steps to confirm the bucket actually shrank.

**This skill drives writes indirectly, via siblings.** It does not call
`execute_procedure` or `execute_batch(dry_run=false)` on its own. Every write
happens inside a sibling skill's own preview / confirm / execute cycle — the
audit's job is to detect, sequence, invoke, and verify. The audit runs
`execute_select_query` directly for detector probes, re-probes, and end-of-run
summaries, and invokes siblings via the `Skill` tool.

**The ONLY hard invariant is each account's CheckedDate.** Every sibling
enforces the lock on writes (a row on/before the active `CheckedDate` is
rejected by `AccountTransaction_Update`). This skill **never moves a
CheckedDate itself** — a lock move triggers the recoil cycle and belongs to
the analyst, not the audit. Rows that come back `lock-blocked` from a sibling
are surfaced in the step's report as-is; the analyst decides whether to move
the lock.

**This skill is custody-agnostic.** Every detector query must run uniformly
over the **entire** `Portfolio.v_AccountTransaction` tape and treat every
custody the same way. Do **not** hard-code custody-specific patterns (BTG's
virtual→application fund cycle, XP's `RENDIMENTOS DE CLIENTES` grammar, JP's
`Cusip` field, XP's compromissada shape, etc.) into a detector's query or its
bucketing logic — those belong inside the **custody-specialised sibling
skills** the audit invokes (`compromissada-fix`, `assetrelated-fix`,
`duplicate-trade-reconcile`, …). If a symptom is only meaningful on one
custody, it does not belong in this audit's detector; raise it in the
specialist skill. The audit's job is to find *any* row of a generic shape
(unmapped identifier, structural duplicate, stuck PENDING, …) regardless of
who booked it, hand it to the specialist for interpretation, and verify the
outcome. A user-supplied `Custody` filter is a scoping choice at call time,
never a built-in assumption of a detector.

## Inputs

- **Period** — default `Date >= DATEADD(day, -7, CAST(GETDATE() AS date))`.
  The user can override with any expression they want (`Date` window,
  `SettlementDate` window, month-to-date, an explicit list of pks, an
  `(Account, Custody)` scope). Echo the resolved period at the start of the
  report.
- **Checks** — default is **all enabled checks**. The user can restrict to a
  subset by name (e.g. "run only the unregistered-assets check").
- **Account / Custody filter** — optional. If omitted, the audit is book-wide.
  If the user gives a short account number, resolve it first (XP onshore
  accounts are **not** zero-padded; BTG / most others **are** zero-padded to 9
  digits — see the sibling skills for the convention per custody):
  `SELECT DISTINCT ClientAccount, Custody FROM Portfolio.v_AccountTransaction
  WHERE ClientAccount LIKE '%<short>%'`.

## Reference resources (read on demand)

| Resource | Read when… |
|---|---|
| [`ayunit://docs/transaction/types`](ayunit://docs/transaction/types) · [`procedure`](ayunit://docs/transaction/procedure) | Refresh the status lifecycle, transaction types, and `AccountTransaction_Update` params before framing a finding. |
| [`ayunit://docs/transaction/fixes`](ayunit://docs/transaction/fixes) | The canonical recipe library — every finding should name the recipe (R1, R3, R7…) that resolves it. |
| [`ayunit://docs/portfolio-creator/pipeline`](ayunit://docs/portfolio-creator/pipeline) | Why an unresolved row matters: which defects block `AccountPosition` and which are noise. |
| [`ayunit://docs/checkeddate/usage`](ayunit://docs/checkeddate/usage) | To flag when a finding sits on/before an active lock (informational — this skill does not move locks). |
| [`ayunit://docs/asset/relationship`](ayunit://docs/asset/relationship) | Framing Check 1 stages 1–2: `Global.Asset` identifier columns (`Cnpj`/`Isin`/`BbgCode`/`Cusip`/`AnbimaCode`/`FundCode`/`ClassCode`/`ExchangeCode`), `Portfolio.v_AssetCustody` translation semantics, and the `Activated` flag that gates the price feeders. |
| [`ayunit://docs/position/reconciliation`](ayunit://docs/position/reconciliation) | Framing Check 1 stage 3: `v_CustodyPosition` LEFT-JOINs `Global.Asset` so untranslated rows surface as `Asset=NULL AssetR=<code>`, and `CustodyPosition_Update @CMD='Update_Missing_Asset'` is the back-fill door. |
| [`ayunit://docs/prices/procedure`](ayunit://docs/prices/procedure) · [`prices/examples`](ayunit://docs/prices/examples) | Framing Check 1 stage 4: `AssetData.Price_Update` semantics and how `asset:asset-price-history` decides which source wins per date. |

## Tools you call directly

- `execute_select_query` — every detector probe, re-probe, and end-of-run
  summary. The audit reads directly.
- `get_view_detail` / `list_views` — confirm columns before writing a new
  detector.
- `Skill` — invoke each sibling skill (Check 1 hand-offs and Check 2's six
  ordered steps). The sibling runs its own preview/confirm/execute cycle.
- **No** `execute_procedure`, **no** `execute_batch(dry_run=false)` — the
  audit does not write directly. Writes only happen inside invoked siblings.
- **Never** `execute_checked_date` — the audit does not move CheckedDate under
  any circumstance.

## The audit + fix cycle

1. **Resolve the period + filters.** Echo them back (`Date` window, account /
   custody scope, checks enabled).
2. **Run Check 1** — the four-stage onboarding-pipeline detector (1a
   `Global.Asset`, 1b `Portfolio.AssetCustody`, 1c `Portfolio.CustodyPosition`,
   1d `AssetData.Price`). For each sub-detector with findings, invoke the
   named sibling in the `asset` plugin (or the top-level
   `routines:new-asset-onboarding` orchestrator for cascading gaps). Re-probe
   after each invocation and report the delta.
3. **Run Check 2** — the six-step sequential fix orchestrator. In order:
   `compromissada-fix` → `assetrelated-fix` → `pending-revalidate` →
   `pending-position-repair` → `duplicate-trade-reconcile` →
   `position-quantity-adjustment`. Detect → skip-if-empty → invoke →
   re-probe → report delta → pause for analyst interrupt → next step.
4. **End with a summary** — one line per check + step with `before → after
   (Δ)` counts, writes performed, rows left `lock-blocked` (analyst
   decision), and rows the sibling declined to touch (low-confidence — analyst
   review). The analyst reads this bottom-up to see what still needs a human.

**Per-step protocol** (applies to every Check 2 step AND every Check 1
sub-detector that has a fix hand-off):

1. **Detect.** Run the detector probe as one `execute_select_query` scoped to
   the audited window. Record the candidate count and pks.
2. **Skip-if-empty.** Count 0 → report `step N (<skill>): 0 candidates,
   skipped`, move on. Never invoke a sibling with an empty scope.
3. **Invoke.** `Skill(<sibling>)` with the pk list / scope as input. Do NOT
   add a second confirmation layer — the sibling's own confirm IS the
   confirmation. Do NOT bypass a sibling's preview/dry-run.
4. **Re-probe.** Re-run the same detector, report `before N → after M`. If
   `M >= N` (bucket didn't shrink), warn the analyst — the sibling wrote
   nothing that resolved the bucket, likely because everything was
   low-confidence / lock-blocked / declined. Pause for the analyst before
   step N+1.
5. **Pause.** Brief pause + short intermediate report so the analyst can
   interrupt if the residual looks wrong. Never chain into the next step
   without giving the analyst a chance to see the current one's outcome.

---

## Check 1 — Onboarding-pipeline gaps on the recent tape

**Symptom.** An asset appearing on the recent tape is "unregistered" until
**four** stages of the onboarding pipeline are complete:

1. **`Global.Asset`** — the canonical security master row exists.
2. **`Portfolio.AssetCustody`** — a per-custody translation row maps this
   custody's identifier(s) to the canonical `Asset`.
3. **`Portfolio.CustodyPosition`** — back-filled: existing custody rows for
   this identifier now carry a resolved `Asset` (they were ingested BEFORE the
   mapping existed and still show `Asset=NULL AssetR=<code>`; the loader does
   not retroactively re-resolve its own history).
4. **`AssetData.Price`** — historical prices are present from the earliest
   trade date to today so `AccountPosition` can price the asset every day the
   pipeline runs `position[D] = transform(position[D-1] + trades[D])`.

Missing any of the four leaves the asset either **stuck as `PENDING`** on
`Portfolio.AccountTransaction` (stages 1/2), **untranslated** on
`Portfolio.CustodyPosition` (stage 3 — per `position/reconciliation`,
`v_CustodyPosition` LEFT-JOINs `Global.Asset`, so untranslated rows stay
visible with `Asset IS NULL`), or **unpriced** downstream (stage 4). This
check surfaces each gap as its own sub-detector so the analyst can hand each
stage off to the right sibling skill.

### Executing Check 1 — single invocation of `routines:new-asset-onboarding`

Check 1 detects the four gaps via its own SQL (sub-detectors 1a–1d below)
and, **if any of them has non-zero findings**, invokes
`routines:new-asset-onboarding` **exactly once** with the audited `(period,
custody_filter, account_filter)` scope. That orchestrator then executes the
full four-stage pipeline via the appropriate `asset:*` and
`account-transaction:pending-revalidate` sibling skills, each with its own
preview/confirm/execute cycle and CheckedDate enforcement. When the
orchestrator returns, re-run every sub-detector and report the per-stage
delta (`1a: before N → after M`, …).

**Skip-if-empty.** If all four sub-detectors return zero candidates, skip
the invocation entirely and report `Check 1: all four stages clean, no
invocation`.

**Reciprocity convention — no loop.** `routines:new-asset-onboarding` uses
Check 1's sub-detector SQL as its own detection phase (this document is the
source of truth for those queries). It must **NOT** re-invoke
`transaction-workday-audit` recursively — that would create the cycle
Check 1 → `new-asset-onboarding` → `transaction-workday-audit` → Check 1.
The audit calls the orchestrator; the orchestrator re-reads the audit's
queries. Both skills' bodies restate this convention (see
`routines:new-asset-onboarding` §"Leaf skills it invokes").

**Sub-detector purpose.** Each sub-detector (1a / 1b / 1c / 1d) serves two
roles simultaneously:

1. **Report** — tells the analyst which of the four stages has residual gaps
   and how many rows / assets are affected, so they can see the shape of
   what the orchestrator is about to fix and veto before invocation.
2. **Detector SQL** — the query itself is what `new-asset-onboarding`
   re-reads to build its own scoping input.

The individual **"Hand-off"** line in each sub-detector below (e.g. 1a →
`asset:asset-register`, 1b → `asset:assetcustody-fill`) is **informational**
— it tells the analyst which sibling `new-asset-onboarding` will delegate to
for that gap. Check 1's executing form does **NOT** invoke those siblings
directly; the analyst can still run them by hand if they want to fix a
single stage in isolation.

Restrict every sub-detector's tape scan to transaction types that carry an
asset (`BUY` / `SELL` / `ASSET RECEIPT` / `ASSET DELIVERY`) — GL / DEPOSIT /
WITHDRAW rows carry `Asset='BRL'` (or the account currency) by design and are
not "assets" in this sense.

### 1a — `Global.Asset` missing (identifier resolves nowhere)

**Symptom.** Tape row is `Asset IS NULL` AND no `Global.v_Asset` row has this
custody's identifier in ANY of the identifier columns catalogued in
`asset/relationship` (`Asset`, `Cnpj`, `Isin`, `BbgCode`, `Cusip`,
`AnbimaCode`, `FundCode`, `ClassCode`, `ExchangeCode`). The security has to be
**registered** before anything else can proceed.

**Query.**

```sql
WITH tape AS (
    SELECT DISTINCT t.Custody, t.AssetCustody, t.CustodyIdentifier
    FROM Portfolio.v_AccountTransaction t
    WHERE t.Date >= DATEADD(day, -7, CAST(GETDATE() AS date))       -- <period>
      AND t.TransactionType IN ('BUY','SELL','ASSET RECEIPT','ASSET DELIVERY')
      AND t.Asset IS NULL
      AND (t.AssetCustody IS NOT NULL OR t.CustodyIdentifier IS NOT NULL)
),
resolved AS (
    SELECT tp.Custody, tp.AssetCustody, tp.CustodyIdentifier,
           MAX(a.Asset) AS ResolvedAsset
    FROM tape tp
    LEFT JOIN Global.v_Asset a
           ON a.Asset        IN (tp.AssetCustody, tp.CustodyIdentifier)
           OR a.Cnpj         IN (tp.AssetCustody, tp.CustodyIdentifier)
           OR a.Isin         IN (tp.AssetCustody, tp.CustodyIdentifier)
           OR a.BbgCode      IN (tp.AssetCustody, tp.CustodyIdentifier)
           OR a.Cusip        IN (tp.AssetCustody, tp.CustodyIdentifier)
           OR a.AnbimaCode   IN (tp.AssetCustody, tp.CustodyIdentifier)
           OR a.FundCode     IN (tp.AssetCustody, tp.CustodyIdentifier)
           OR a.ClassCode    IN (tp.AssetCustody, tp.CustodyIdentifier)
           OR a.ExchangeCode IN (tp.AssetCustody, tp.CustodyIdentifier)
    GROUP BY tp.Custody, tp.AssetCustody, tp.CustodyIdentifier
)
SELECT
    t.Custody, t.AssetCustody, t.CustodyIdentifier,
    MIN(t.Date)                     AS FirstSeen,
    MAX(t.Date)                     AS LastSeen,
    COUNT(*)                        AS [RowCount],
    COUNT(DISTINCT t.ClientAccount) AS Accounts,
    MIN(t.pk_AccountTransactionID)  AS SamplePk,
    MAX(t.GeneralLedgerDescription) AS GeneralLedgerDescriptionSample
FROM Portfolio.v_AccountTransaction t
JOIN resolved r
  ON r.Custody = t.Custody
 AND ISNULL(r.AssetCustody,'')      = ISNULL(t.AssetCustody,'')
 AND ISNULL(r.CustodyIdentifier,'') = ISNULL(t.CustodyIdentifier,'')
WHERE t.Date >= DATEADD(day, -7, CAST(GETDATE() AS date))           -- <period>
  AND t.TransactionType IN ('BUY','SELL','ASSET RECEIPT','ASSET DELIVERY')
  AND t.Asset IS NULL
  AND r.ResolvedAsset IS NULL                                       -- nothing matched
GROUP BY t.Custody, t.AssetCustody, t.CustodyIdentifier
ORDER BY t.Custody, LastSeen DESC;
```

> The identifier probe is deliberately book-wide (every identifier column on
> `Global.Asset` × both tape columns) so the same query works uniformly on
> every custody without per-custody branching. If a new identifier column is
> added to `Global.Asset`, widen this `LEFT JOIN` rather than forking the
> check per custody.

**Report.** One row per `(Custody, AssetCustody, CustodyIdentifier)` tuple.
Include the `SamplePk` so the analyst can jump straight to a representative
row and inspect its `RawTransaction` (Bloomberg-side enrichment often needs an
ISIN / CUSIP only present there).

**Hand-off.**
- `asset:asset-lookup` — safety pre-check that the asset isn't already
  registered under a different identifier (avoids duplicate registrations).
- `asset:register-br-funds` — for Brazilian fund CNPJs (uses the ANBIMA feed).
- `asset:asset-enrich-from-bbg` → `asset:asset-register` — for BBG-identifiable
  securities (equities, options, offshore funds, treasuries, bonds).
- `asset:asset-register` — everything else (manual registration).
- After registration, the same identifier will typically also fail 1b (no
  mapping yet). Continue there.

**Verification.** Re-run 1a. The bucket should shrink to only the tuples the
analyst deliberately deferred.

### 1b — `Portfolio.AssetCustody` mapping missing (asset exists but not translated)

**Symptom.** Tape row is `Asset IS NULL`, but a `Global.Asset` row DOES match
one of the tape's identifiers. The gap is the per-custody translation row in
`Portfolio.AssetCustody` — without it, the loader's auto-match cannot complete
even though the security is registered.

**Query.** Same shell as 1a, but keep the rows where the identifier DID
resolve, and expose the resolved `Asset` code + a resolution-ambiguity
indicator.

```sql
WITH tape AS (
    SELECT DISTINCT t.Custody, t.AssetCustody, t.CustodyIdentifier
    FROM Portfolio.v_AccountTransaction t
    WHERE t.Date >= DATEADD(day, -7, CAST(GETDATE() AS date))       -- <period>
      AND t.TransactionType IN ('BUY','SELL','ASSET RECEIPT','ASSET DELIVERY')
      AND t.Asset IS NULL
      AND (t.AssetCustody IS NOT NULL OR t.CustodyIdentifier IS NOT NULL)
),
resolved AS (
    SELECT tp.Custody, tp.AssetCustody, tp.CustodyIdentifier,
           MIN(a.Asset)            AS ResolvedAsset,
           COUNT(DISTINCT a.Asset) AS ResolvedCount
    FROM tape tp
    LEFT JOIN Global.v_Asset a
           ON a.Asset        IN (tp.AssetCustody, tp.CustodyIdentifier)
           OR a.Cnpj         IN (tp.AssetCustody, tp.CustodyIdentifier)
           OR a.Isin         IN (tp.AssetCustody, tp.CustodyIdentifier)
           OR a.BbgCode      IN (tp.AssetCustody, tp.CustodyIdentifier)
           OR a.Cusip        IN (tp.AssetCustody, tp.CustodyIdentifier)
           OR a.AnbimaCode   IN (tp.AssetCustody, tp.CustodyIdentifier)
           OR a.FundCode     IN (tp.AssetCustody, tp.CustodyIdentifier)
           OR a.ClassCode    IN (tp.AssetCustody, tp.CustodyIdentifier)
           OR a.ExchangeCode IN (tp.AssetCustody, tp.CustodyIdentifier)
    GROUP BY tp.Custody, tp.AssetCustody, tp.CustodyIdentifier
)
SELECT
    t.Custody, t.AssetCustody, t.CustodyIdentifier,
    r.ResolvedAsset, r.ResolvedCount,
    MIN(t.Date)                     AS FirstSeen,
    MAX(t.Date)                     AS LastSeen,
    COUNT(*)                        AS [RowCount],
    COUNT(DISTINCT t.ClientAccount) AS Accounts,
    MIN(t.pk_AccountTransactionID)  AS SamplePk
FROM Portfolio.v_AccountTransaction t
JOIN resolved r
  ON r.Custody = t.Custody
 AND ISNULL(r.AssetCustody,'')      = ISNULL(t.AssetCustody,'')
 AND ISNULL(r.CustodyIdentifier,'') = ISNULL(t.CustodyIdentifier,'')
WHERE t.Date >= DATEADD(day, -7, CAST(GETDATE() AS date))           -- <period>
  AND t.TransactionType IN ('BUY','SELL','ASSET RECEIPT','ASSET DELIVERY')
  AND t.Asset IS NULL
  AND r.ResolvedAsset IS NOT NULL
GROUP BY t.Custody, t.AssetCustody, t.CustodyIdentifier,
         r.ResolvedAsset, r.ResolvedCount
ORDER BY t.Custody, LastSeen DESC;
```

> **Ambiguity guard.** `ResolvedCount > 1` means the tape's identifier
> matches more than one `Global.Asset` row (e.g. a CNPJ shared by two
> registered class codes, an ISIN reused). Flag those rows for analyst review
> — do NOT let `assetcustody-fill` auto-pick a target.

**Report.** Same shape as 1a plus the `ResolvedAsset` column (the canonical
code the identifier resolves to) so the analyst / `assetcustody-fill` knows
the map target without a second lookup.

**Hand-off.** `asset:assetcustody-fill` — adds the `Portfolio.AssetCustody`
row(s), runs `Portfolio.CustodyPosition_Update @CMD='Update_Missing_Asset'` to
back-fill any existing custody rows (see 1c), then hands the affected
`PENDING` transaction pks to `pending-revalidate`. One skill covers 1b and 1c
because the same fix resolves both symptoms.

**Verification.** Re-run 1b. The bucket should be empty (excluding
`ResolvedCount > 1` rows the analyst is still triaging).

### 1c — `Portfolio.CustodyPosition` still has `Asset=NULL` on the audited scope

**Symptom.** Even after the mapping in `Portfolio.AssetCustody` is added,
`CustodyPosition` rows that were ingested BEFORE the map still carry
`Asset=NULL AssetR=<code>` — the loader doesn't retroactively re-resolve its
own history. Per `position/reconciliation`, `v_CustodyPosition` LEFT-JOINs
`Global.Asset`, so these rows stay visible with `Asset IS NULL` until
`Portfolio.CustodyPosition_Update @CMD='Update_Missing_Asset'` back-fills
them. Reconciliation to `AccountPosition` can't happen for these rows until
they're back-filled (no `Asset` → no key to pair on).

Scope: the audited period × the `(Account, Custody)` pairs that appear in the
audited tape (per part-1 confirmation — book-wide backlog is out of scope,
even though `Update_Missing_Asset` runs custody-wide).

**Query.**

```sql
WITH scope AS (
    SELECT DISTINCT t.ClientAccount AS Account, t.Custody
    FROM Portfolio.v_AccountTransaction t
    WHERE t.Date >= DATEADD(day, -7, CAST(GETDATE() AS date))       -- <period>
)
SELECT
    cp.Custody, cp.Account,
    cp.AssetR, cp.IsinR, cp.AnbimaCodeR,
    MIN(cp.Date)     AS FirstSeen,
    MAX(cp.Date)     AS LastSeen,
    COUNT(*)         AS [RowCount],
    SUM(cp.Quantity) AS TotalQuantity,
    SUM(cp.Value)    AS TotalValue
FROM Portfolio.v_CustodyPosition cp
JOIN scope s ON s.Account = cp.Account AND s.Custody = cp.Custody
WHERE cp.Date >= DATEADD(day, -7, CAST(GETDATE() AS date))          -- <period>
  AND cp.Asset  IS NULL
  AND cp.AssetR IS NOT NULL
GROUP BY cp.Custody, cp.Account, cp.AssetR, cp.IsinR, cp.AnbimaCodeR
ORDER BY cp.Custody, cp.Account, LastSeen DESC;
```

**Report.** One row per `(Custody, Account, AssetR)`. Include `IsinR` /
`AnbimaCodeR` where present — those often disambiguate the `AssetR` when it's
a numeric internal code.

**Hand-off.** `asset:assetcustody-fill` (runs `Update_Missing_Asset` after
adding/verifying the mapping — one skill covers 1b and 1c). Rows that persist
after that fix mean the `AssetR` genuinely has no mapping yet — cycle back to
1a/1b.

**Verification.** Re-run 1c. The bucket should be empty for the audited scope.

### 1d — `AssetData.Price` backfill gap from earliest trade date to today

**Symptom.** Asset is registered and mapped, but `AssetData.v_Price` doesn't
cover the full range `[MIN(tape.Date), today]` — some business days lack a
price row, so `AccountPosition` uses stale or zero-filled prices. Per the
pipeline invariant `position[D] = transform(position[D-1] + trades[D])`,
every business day in the span matters.

Scope: assets that appear as `Asset IS NOT NULL` on the audited window,
restricted to `Global.Asset.Activated = 1` — per `asset/relationship`,
`Activated` gates whether the price feeders still fetch this asset. The tape
scan for the span is **unbounded on Date** (per part-1 confirmation: use the
asset's full trade history to know the earliest date needing prices, not just
dates inside the audit window).

**Query.**

```sql
WITH window_assets AS (
    SELECT DISTINCT t.Asset
    FROM Portfolio.v_AccountTransaction t
    WHERE t.Date >= DATEADD(day, -7, CAST(GETDATE() AS date))       -- <period>
      AND t.TransactionType IN ('BUY','SELL','ASSET RECEIPT','ASSET DELIVERY')
      AND t.Asset IS NOT NULL
),
tape_span AS (
    SELECT t.Asset,
           MIN(t.Date) AS EarliestTapeDate,
           MAX(t.Date) AS LatestTapeDate,
           COUNT(*)    AS TapeRowCount
    FROM Portfolio.v_AccountTransaction t
    WHERE t.Asset IN (SELECT Asset FROM window_assets)
      AND t.TransactionType IN ('BUY','SELL','ASSET RECEIPT','ASSET DELIVERY')
    GROUP BY t.Asset                                                -- unbounded on Date
),
covered AS (
    SELECT p.Asset,
           MIN(p.Date)           AS EarliestPriceDate,
           MAX(p.Date)           AS LatestPriceDate,
           COUNT(DISTINCT p.Date) AS PriceDatesCovered   -- distinct dates, not rows
    FROM AssetData.v_Price p
    JOIN tape_span ts
      ON ts.Asset = p.Asset
     AND p.Date  >= ts.EarliestTapeDate
     AND p.Date  <= CAST(GETDATE() AS date)
    GROUP BY p.Asset
)
SELECT
    ts.Asset,
    a.Description, a.AssetGroup, a.Currency, a.Activated,
    ts.EarliestTapeDate, ts.LatestTapeDate, ts.TapeRowCount,
    c.EarliestPriceDate, c.LatestPriceDate,
    ISNULL(c.PriceDatesCovered, 0) AS PriceDatesCovered,
    -- approx business days in [EarliestTapeDate, today]: weekends excluded, BR holidays NOT
    (DATEDIFF(day,  ts.EarliestTapeDate, CAST(GETDATE() AS date)) + 1
     - 2 * DATEDIFF(week, ts.EarliestTapeDate, CAST(GETDATE() AS date))) AS ApproxBusinessDaysExpected,
    -- clamped: negatives were an artifact of multi-source dates being counted with COUNT(*)
    CASE WHEN ((DATEDIFF(day,  ts.EarliestTapeDate, CAST(GETDATE() AS date)) + 1
                - 2 * DATEDIFF(week, ts.EarliestTapeDate, CAST(GETDATE() AS date)))
               - ISNULL(c.PriceDatesCovered, 0)) < 0 THEN 0
         ELSE ((DATEDIFF(day,  ts.EarliestTapeDate, CAST(GETDATE() AS date)) + 1
                - 2 * DATEDIFF(week, ts.EarliestTapeDate, CAST(GETDATE() AS date)))
               - ISNULL(c.PriceDatesCovered, 0))
    END AS ApproxGapCount,
    CASE
        WHEN ISNULL(c.PriceDatesCovered, 0) = 0                                 THEN 'full_backfill'
        WHEN c.EarliestPriceDate > ts.EarliestTapeDate                          THEN 'full_backfill'
        WHEN c.LatestPriceDate   < DATEADD(day, -3, CAST(GETDATE() AS date))    THEN 'recent_gap'
        ELSE                                                                         'other'
    END AS tier
FROM tape_span ts
JOIN Global.v_Asset a ON a.Asset = ts.Asset
LEFT JOIN covered c   ON c.Asset = ts.Asset
WHERE a.Activated = 1
ORDER BY ApproxGapCount DESC, ts.Asset;
```

> **Approximation note.** `ApproxBusinessDaysExpected` excludes weekends via a
> portable T-SQL formula but does NOT exclude Brazilian holidays, so
> `ApproxGapCount` slightly overstates the true gap. It's a "which assets
> need backfill first" ranking, not an authoritative missing-date list. The
> authoritative source-priority backfill lives in `asset:asset-price-history`.
> `PriceDatesCovered` uses `COUNT(DISTINCT p.Date)` so it isn't inflated by
> assets carrying multiple price rows per date (e.g. two sources on the same
> day); `ApproxGapCount` is clamped at 0 for the same reason.

**Report.** One row per asset. Sort by `ApproxGapCount` desc. Two tiers so
the analyst knows what's a full backfill vs a tail-only patch:

| Tier | Rule | Meaning |
|---|---|---|
| **Full backfill** | `PriceRowCount = 0` OR `EarliestPriceDate > EarliestTapeDate` | Missing at (or from) the front — a full backfill run is needed. |
| **Recent gap** | `LatestPriceDate < today - 3 business days` | Front covered, tail stale — a short backfill will patch it. |

**Hand-off.** `asset:asset-price-history` — takes the asset list plus the
`[EarliestTapeDate, today]` range and runs its source-priority chain
(`AssetData.v_Price` have-set → `AssetDataDB.v_MarketData` →
`AssetDataDB.v_Price` → `Portfolio.v_CustodyPosition × PriceFactor`) to fill
every missing date without overwriting existing rows.

**Verification.** Re-run 1d for the same asset. `ApproxGapCount` should drop
to near zero (residual = BR holidays the formula didn't discount).

### Sidebar — Asset FK broken (rare, outside the pipeline story)

If the tape row has `Asset IS NOT NULL` but the code doesn't exist in
`Global.v_Asset`, the FK is corrupted — someone edited the row to a code no
longer registered, or the underlying `Global.Asset` was hard-deleted (which
the schema tries to prevent via inbound FKs). Not a pipeline gap; escalate to
master data and check for a recent hand-edit of that pk.

```sql
SELECT DISTINCT
    t.Asset, t.Custody,
    MIN(t.Date) AS FirstSeen, MAX(t.Date) AS LastSeen,
    COUNT(*)    AS [RowCount],
    MIN(t.pk_AccountTransactionID) AS SamplePk
FROM Portfolio.v_AccountTransaction t
LEFT JOIN Global.v_Asset a ON a.Asset = t.Asset
WHERE t.Date >= DATEADD(day, -7, CAST(GETDATE() AS date))           -- <period>
  AND t.Asset IS NOT NULL
  AND t.Asset <> 'BRL'
  AND a.Asset IS NULL
GROUP BY t.Asset, t.Custody
ORDER BY LastSeen DESC;
```

---

## Check 2 — Sequential defect-resolution orchestrator

Check 2 is the workday driver. It invokes the six defect-fix sibling skills in
`account-transaction` in a fixed order — each step either unblocks or narrows
the input to the next — re-probing the tape between steps to confirm the
target bucket actually shrank. Each sibling handles its own preview / confirm
/ execute cycle and CheckedDate lock enforcement; the audit itself does not
write directly and does not move locks.

### Ordering rationale

| # | Skill | Why in this position |
|---|---|---|
| 1 | `compromissada-fix` | XP's repo-shape normalisation is a distinct ingestion pattern that would otherwise inflate downstream PENDING / duplicate buckets. Fixing it first cleans the input for every following step. |
| 2 | `assetrelated-fix` | Fills `AssetRelated` on GL RECEIPT `INTEREST`/`DIVIDEND` (income) rows — a disjoint blocker (`missing: AssetRelated`) that `pending-revalidate` deliberately does NOT handle. Must run before `pending-revalidate` or those rows are stranded. |
| 3 | `pending-revalidate` | Clears 3-A / 3-B / 3-C blockers (`missing: Asset` / `Price` / `Quantity`) whose master-data prerequisites Check 1 has just unblocked. Runs after 1 and 2 so its input is clean and disjoint. |
| 4 | `pending-position-repair` | Position-delta inference for the 3-Z-unclassified residuum surviving step 3 — PENDINGs whose `RawTransaction` carries no valid asset identifier but whose `AccountPosition ↔ CustodyPosition` delta reveals the intended `(Asset, direction)`. |
| 5 | `duplicate-trade-reconcile` | With PENDINGs cleared by 3 / 4, the cluster detector sees the true `VALIDATED` / `UPDATED` book, so the reconciliation against `Portfolio.CustodyPosition` isn't polluted by PENDING noise. |
| 6 | `position-quantity-adjustment` | Structural plug for the residual quantity / cash breaks the previous five didn't close (fractional dust, rounding gaps). Runs last as the backstop. |

### Pre-flight — Check 1 residual gate

If Check 1 still has residual findings (1a / 1b / 1c / 1d) after its own
orchestration pass, step 3 (`pending-revalidate`) will silently skip the
affected pks — the assets it needs are still not master-data-ready. Report
the residual count per Check 1 sub-detector, then ask the analyst *"Check 1
has N unresolved rows that step 3 will skip — continue anyway?"* and proceed
only on confirmation.

Do NOT hard-gate — the analyst may deliberately defer a registration
(waiting on legal, ambiguous CNPJ, low-confidence enrichment). Do NOT auto-run
Check 1's hand-off from inside Check 2 — that would be too much magic packed
into a single call.

### Step 1 — `compromissada-fix` (COMPROMISSADA repo normalisation)

**Detector probe.** Rows in the audited window whose shape *may* be a
compromissada leg — an XP feed booking a repo trade with `Quantity =
underlying-debenture unit count` and `Price = unit price` instead of the
cash-like `Quantity = value, Price = 1`. Detected at superset granularity
(`Asset` resolves to a repo instrument in `Global.Asset`, or the GL
description names it); the sibling itself pairs BUY↔SELL legs and confirms
which rows actually need the R6 unit-vs-value inversion fix.

```sql
SELECT
    t.pk_AccountTransactionID AS Pk,
    t.Custody, t.ClientAccount, t.Asset, t.TransactionType, t.Date,
    t.Quantity, t.Price, t.Value, t.Status,
    t.GeneralLedgerDescription
FROM Portfolio.v_AccountTransaction t
LEFT JOIN Global.v_Asset a ON a.Asset = t.Asset
WHERE t.Date >= DATEADD(day, -7, CAST(GETDATE() AS date))           -- <period>
  AND t.TransactionType IN ('BUY','SELL')
  AND (a.SecurityType LIKE '%COMPROMISSADA%'
    OR a.Product      LIKE '%COMPROMISSADA%'
    OR t.GeneralLedgerDescription LIKE '%COMPROMISSADA%')
  AND t.Status IN ('PENDING','VALIDATED','UPDATED')
ORDER BY t.Date DESC, t.pk_AccountTransactionID;
```

> The detector is a **lightweight superset**. A false positive here just
> means the sibling reports "already correctly shaped" for that pk and
> moves on. Do NOT try to reproduce the sibling's classifier in this query.

**Invocation.** `Skill(account-transaction:compromissada-fix)` with the pk
list (or the `(Account, Custody, Date-window)` scope).

**Re-probe.** Re-run the detector. Expect the sibling to have converted the
mis-shaped legs (`Status='UPDATED'`, `Price=1`, `Quantity=value`) so those
rows now fail the "looks mis-shaped" heuristic and drop out. Report the
delta.

### Step 2 — `assetrelated-fix` (attribute income to the originating asset)

**Detector probe.** Two shapes, both surfaced by the sibling:
- `VALIDATED`/`UPDATED` GL RECEIPT `INTEREST`/`DIVIDEND` rows with
  `AssetRelated IS NULL` — the cash flow disappears from asset return
  attribution.
- `PENDING` income rows blocked on the loader diagnostic `missing:
  AssetRelated` — the 3-D bucket from the classifier (see Step 3 below for
  the full grammar).

```sql
SELECT
    t.pk_AccountTransactionID AS Pk,
    t.Custody, t.ClientAccount, t.Date, t.TransactionType,
    t.Asset, t.AssetRelated, t.GeneralLedgerType, t.GeneralLedgerDescription,
    t.Status,
    LEFT(CAST(t.SystemCheck AS varchar(1000)), 300) AS SystemCheck_head
FROM Portfolio.v_AccountTransaction t
WHERE t.Date >= DATEADD(day, -7, CAST(GETDATE() AS date))           -- <period>
  AND (
        (t.TransactionType = 'GENERAL LEDGER RECEIPT'
         AND t.GeneralLedgerType IN ('INTEREST','DIVIDEND')
         AND t.AssetRelated IS NULL
         AND t.Status IN ('VALIDATED','UPDATED'))
     OR (t.Status = 'PENDING'
         AND CAST(t.SystemCheck AS varchar(1000)) LIKE '%missing: AssetRelated%')
  )
ORDER BY t.Date DESC, t.pk_AccountTransactionID;
```

**Invocation.** `Skill(account-transaction:assetrelated-fix)` with the pk
list — the sibling parses the description via layouts A / B / C (XP
`RENDIMENTOS DE CLIENTES <ticker>`, XP `Devolução Tx de Distr <fund>`,
JP/international ISIN in description or CUSIP in `RawTransaction`), confirms
against the account's holding universe / a global ISIN-CUSIP index, and
promotes each high-confidence match to `UPDATED`.

**Re-probe.** Same query. Report delta.

### Step 3 — `pending-revalidate` (re-fire loader validators on unblocked PENDINGs)

**Detector probe.** The `SystemCheck` classifier (formerly the standalone
Check 3) — the loader writes a structured diagnostic on every row it can't
fully validate:

```
[…partial fixes it applied…]; Revalidation: Status remains PENDING (missing: <field-list>)
```

`<field-list>` is one or more of: `Asset`, `AssetRelated`, `Price`, `Value`,
`Quantity`, `TransactionType`. Parsing that grammar buckets every PENDING
row; the resolvability probe joins master data to check whether the blocker
can be cleared today.

| Bucket | `SystemCheck` signal | Resolvable-now probe | Where the fix goes |
|---|---|---|---|
| **3-A · Asset unresolved** | `missing: Asset` (often preceded by `Fail to get Asset Register`) | `Portfolio.v_AssetCustody` has a mapping for `(Custody, AssetCustody \| CustodyIdentifier)` today | this step (`pending-revalidate`) |
| **3-B · Price missing** | `missing: Price` | `AssetData.v_Price` has a row for `(Asset, Date)`. Note: loader auto-fill only fires when `Price`, `Value`, `ValueGross` are ALL 0/NULL AND `Quantity ≠ 0`; if `Value` is populated, `pending-revalidate` derives `Price = ABS(Value / (Quantity × ContractSize))` instead | this step |
| **3-C · Quantity missing** | `missing: Quantity` | `Value` and `Price` are populated → Quantity is derivable | this step |
| **3-D · AssetRelated missing** | `missing: AssetRelated` | not this step — handled by **step 2** (`assetrelated-fix`), which already ran | (skip in step 3 — resolved earlier or reported for analyst) |
| **3-Z · Special** | `Invalid TransactionType`, `MS activity loader: DEBIT CARD`, come-cotas (`Obs LIKE '%COME COTAS%'`), `SOURCE: FUND_ACCOUNT_STATEMENT` with three-way missing, `3-Z-unclassified` (no diagnosis) | not auto-resolvable via re-validate | (handled by **step 4** for unclassified, else analyst) |

Classifier + resolvability probe:

```sql
WITH pending AS (
    SELECT
        t.pk_AccountTransactionID AS Pk, t.Date, t.SettlementDate,
        t.ClientAccount, t.Custody, t.TransactionType,
        t.Asset, t.AssetCustody, t.CustodyIdentifier, t.AssetRelated,
        t.Quantity, t.Price, t.Value, t.ValueGross,
        CAST(t.SystemCheck AS varchar(1000)) AS SystemCheck_txt,
        CAST(t.Obs         AS varchar(500))  AS Obs_txt
    FROM Portfolio.v_AccountTransaction t
    WHERE t.Date >= DATEADD(day, -7, CAST(GETDATE() AS date))       -- <period>
      AND t.Status = 'PENDING'
),
classified AS (
    SELECT p.*,
        CASE
            WHEN p.Obs_txt         LIKE '%COME COTAS%'              THEN '3-Z-come-cotas'
            WHEN p.SystemCheck_txt LIKE '%Invalid TransactionType%' THEN '3-Z-invalid-txtype'
            WHEN p.SystemCheck_txt LIKE '%DEBIT CARD%'              THEN '3-Z-debit-card'
            WHEN p.SystemCheck_txt LIKE '%missing: AssetRelated%'   THEN '3-D-assetrelated'
            WHEN p.SystemCheck_txt LIKE '%missing: %Asset%'
             AND p.SystemCheck_txt LIKE '%missing: %Quantity%'
             AND p.SystemCheck_txt LIKE '%missing: %Price%'         THEN '3-Z-three-way-missing'
            WHEN p.SystemCheck_txt LIKE '%missing: %Asset%'         THEN '3-A-asset'
            WHEN p.SystemCheck_txt LIKE '%missing: %Quantity%'      THEN '3-C-quantity'
            WHEN p.SystemCheck_txt LIKE '%missing: %Price%'         THEN '3-B-price'
            WHEN p.SystemCheck_txt IS NULL AND p.Asset IS NULL
             AND (p.AssetCustody IS NOT NULL OR p.CustodyIdentifier IS NOT NULL)
                                                                    THEN '3-A-asset'  -- loader wrote no diagnosis (rare)
            ELSE                                                         '3-Z-unclassified'
        END AS Bucket
    FROM pending p
)
SELECT
    c.Bucket, c.Pk, c.Date, c.ClientAccount, c.Custody, c.TransactionType,
    c.Asset, c.AssetCustody, c.CustodyIdentifier, c.Quantity, c.Price, c.Value,
    CASE c.Bucket
        WHEN '3-A-asset'    THEN CASE WHEN ac.Asset IS NOT NULL             THEN 1 ELSE 0 END
        WHEN '3-B-price'    THEN CASE WHEN pr.Asset IS NOT NULL             THEN 1 ELSE 0 END
        WHEN '3-C-quantity' THEN CASE WHEN c.Value IS NOT NULL AND c.Price IS NOT NULL AND c.Price <> 0
                                                                            THEN 1 ELSE 0 END
        ELSE NULL
    END                       AS ResolvableNow,
    ac.Asset                  AS ResolvedAsset,
    LEFT(c.SystemCheck_txt, 300) AS SystemCheck_head
FROM classified c
LEFT JOIN Portfolio.v_AssetCustody ac
       ON c.Bucket = '3-A-asset'
      AND ac.Custody = c.Custody
      AND (ac.TickerCustody  = c.AssetCustody
        OR ac.TickerCustody2 = c.AssetCustody
        OR ac.TickerCustody  = c.CustodyIdentifier
        OR ac.TickerCustody2 = c.CustodyIdentifier)
LEFT JOIN AssetData.v_Price pr
       ON c.Bucket = '3-B-price'
      AND pr.Asset = c.Asset AND pr.Date = c.Date
ORDER BY c.Bucket, c.Custody, c.Date DESC;
```

> **3-A ambiguity guard.** When the mapping join returns > 1 `ac.Asset` for
> the same `(Custody, identifier)`, that's a mapping conflict — mark the row
> as `3-Z-ambiguous-mapping` (add `COUNT(DISTINCT ac.Asset) OVER (PARTITION
> BY c.Pk) > 1` in the outer SELECT) and exclude it from step 3's invocation
> set. Rare but real.

**Invocation.** `Skill(account-transaction:pending-revalidate)` with the pk
list filtered to `Bucket IN ('3-A-asset','3-B-price','3-C-quantity') AND
ResolvableNow = 1`. The sibling re-invokes `AccountTransaction_Update
@CMD='U'` (SELECT-first-merge, lock-gated, atomic batch) so the procedure's
built-in auto-validators re-fire.

Rows in the classifier that are NOT part of the invocation set:
- `Bucket='3-D-assetrelated'` — already handled in step 2. If any remain,
  the sibling declined them (low-confidence layout match) — surface for
  analyst.
- `Bucket IN ('3-A','3-B','3-C') AND ResolvableNow = 0` — blocker still
  active. Loop back to Check 1 (typically 1a/1b/1d).
- Any `3-Z-*` — carried into step 4 (`pending-position-repair`) if it fits
  its shape; otherwise surfaced for analyst review.

**Re-probe.** Re-run the classifier. Any pk that persists as `PENDING` with
the **same** `SystemCheck_head` after invocation means the re-validation
didn't take — surface it as a bug against the detector (the resolvable-now
probe was wrong or the row hit a validator not modelled here), not as a
rerun.

### Step 4 — `pending-position-repair` (position-delta inference for 3-Z residuum)

**Detector probe.** PENDING rows the classifier bucketed as
`3-Z-unclassified` or `3-Z-three-way-missing` that also lack any usable
identifier (`Asset`, `AssetCustody`, `CustodyIdentifier` all NULL) — the
loader has no diagnostic to act on, but the `AccountPosition ↔
CustodyPosition` delta on the same account/date may reveal the intended
`(Asset, direction)`. `pending-position-repair` scores each pk (HIGH / MED
/ LOW) via that evidence and writes only on HIGH-confidence.

```sql
WITH pending AS (
    SELECT
        t.pk_AccountTransactionID AS Pk,
        t.ClientAccount, t.Custody, t.Date, t.TransactionType,
        t.Asset, t.AssetCustody, t.CustodyIdentifier, t.Quantity, t.Value,
        CAST(t.SystemCheck AS varchar(1000)) AS SystemCheck_txt
    FROM Portfolio.v_AccountTransaction t
    WHERE t.Date >= DATEADD(day, -7, CAST(GETDATE() AS date))       -- <period>
      AND t.Status = 'PENDING'
)
SELECT p.Pk, p.ClientAccount, p.Custody, p.Date, p.TransactionType,
       p.Asset, p.AssetCustody, p.CustodyIdentifier, p.Quantity, p.Value,
       LEFT(p.SystemCheck_txt, 300) AS SystemCheck_head
FROM pending p
WHERE p.Asset          IS NULL
  AND p.AssetCustody   IS NULL
  AND p.CustodyIdentifier IS NULL
  AND (p.SystemCheck_txt IS NULL
    OR p.SystemCheck_txt NOT LIKE '%missing: Asset%'
    OR p.SystemCheck_txt LIKE '%SOURCE: FUND_ACCOUNT_STATEMENT%')
ORDER BY p.Date DESC, p.Pk;
```

**Invocation.** `Skill(account-transaction:pending-position-repair)` **per
pk** with its position-delta evidence bundle — the sibling needs the delta
context per row, so this step is per-pk rather than batched.

**Re-probe.** Same query. HIGH-confidence pks should promote to `UPDATED`
and drop out. MED / LOW pks stay `PENDING` — surface them as "candidates
for analyst review" with the sibling's ranked-candidates report.

### Step 5 — `duplicate-trade-reconcile` (structural duplicates against custody)

**Detector probe — same-key clusters.** Two rows are a candidate duplicate
pair when they share **all** of: `ClientAccount`, `Custody`, `Asset`,
`TransactionType`, `Date`, `ABS(Quantity)`. `Value` / `Price` are
deliberately NOT in the cluster key — a re-sent trade with a revised NAV
keeps the same quantity but shifts the price and would otherwise slip through.
Cluster-internal `Value` variance is reported as a signal (identical →
plain double load; drifted → likely price restatement). A ±1-day widening is
NOT built in book-wide (per-custody consecutive-day restatements are
resolved by the sibling against `CustodyPosition`, not here).

With steps 3 / 4 having cleared PENDINGs, the cluster detector sees the
true `VALIDATED` / `UPDATED` book.

```sql
WITH clusters AS (
    SELECT
        t.ClientAccount, t.Custody, t.Asset, t.TransactionType, t.Date,
        ABS(t.Quantity) AS AbsQty,
        COUNT(*)                          AS [RowCount],
        MIN(t.Value)                      AS MinValue,
        MAX(t.Value)                      AS MaxValue,
        SUM(CASE WHEN t.Status IN ('VALIDATED','UPDATED') THEN 1 ELSE 0 END) AS ValidatedCount,
        SUM(CASE WHEN t.Status = 'PENDING' THEN 1 ELSE 0 END)                AS PendingCount,
        STRING_AGG(CAST(t.pk_AccountTransactionID AS varchar(32)), ',')      AS Pks
    FROM Portfolio.v_AccountTransaction t
    WHERE t.Date >= DATEADD(day, -7, CAST(GETDATE() AS date))       -- <period>
      AND t.TransactionType IN ('BUY','SELL','ASSET RECEIPT','ASSET DELIVERY')
      AND t.Asset IS NOT NULL
      AND t.Quantity IS NOT NULL AND t.Quantity <> 0
      AND t.Status IN ('PENDING','VALIDATED','UPDATED')
    GROUP BY t.ClientAccount, t.Custody, t.Asset, t.TransactionType, t.Date, ABS(t.Quantity)
    HAVING COUNT(*) > 1
)
SELECT
    ClientAccount, Custody, Asset, TransactionType, Date, AbsQty,
    RowCount, ValidatedCount, PendingCount,
    MinValue, MaxValue,
    CASE
        WHEN ValidatedCount >= 2                       THEN 'A - position-impacting'
        WHEN ValidatedCount >= 1 AND PendingCount >= 1 THEN 'B - mixed'
        WHEN ValidatedCount  = 0 AND PendingCount >= 2 THEN 'C - pending-only'
        ELSE                                                'D - other'
    END AS Bucket,
    CASE
        WHEN MinValue = MaxValue THEN 'identical-value  (plain double load)'
        WHEN MinValue <> 0 AND ABS(MaxValue - MinValue) / ABS(MinValue) < 0.02
                                 THEN 'near-identical   (double load w/ minor fx / rounding)'
        ELSE                          'drifted-value    (likely price restatement)'
    END AS ValueSignal,
    Pks
FROM clusters
ORDER BY Custody, ClientAccount, Date DESC;
```

**Invocation.** `Skill(account-transaction:duplicate-trade-reconcile)` per
cluster (pass the cluster's `Pks` or the `(Account, Custody, Asset, Date)`
scope). Pass the `ValueSignal` through as a hint so the sibling picks the
right per-custody restatement policy (drifted-value → survivor is the
restated leg). Prioritise Bucket A (already double-counting in
`AccountPosition`).

**Re-probe.** Re-run the cluster query. Target clusters should be gone or
reduced to `RowCount = 1`. Clusters that persist mean the sibling flagged
them for analyst review — surface its per-cluster report.

### Step 6 — `position-quantity-adjustment` (absorb residual dust)

**Detector probe.** `CustodyPosition` vs `AccountPosition` per-`(Account,
Date, Asset)` quantity break in the audited scope. Split by asset kind
because the fix shape differs (per the user's convention that non-cash uses
`BUY`/`SELL` and cash uses `GL RECEIPT`/`GL DELIVERY`):

- **Non-cash** (`AssetGroup ≠ 'Currency'`) → sibling books `BUY` (custody >
  ours) / `SELL` (custody < ours) for `|Δ|` with no cash leg (`Value=0`,
  `PriceExFee`=price from `AssetData.v_Price`, `Price=0`).
- **Cash** (`AssetGroup = 'Currency'`, i.e. `BRL` / `USD` / `EUR` / …) →
  sibling books `GENERAL LEDGER RECEIPT` (custody > ours) / `GENERAL LEDGER
  DELIVERY` (custody < ours) for `|Δ|` with full cash impact (`Value =
  Quantity = ValueGross = |Δ|`, `Price=1`).

Tolerance is the **sibling's** decision, not the audit's. The detector
reports every non-zero delta on the audited scope; the sibling filters to
`|Δ| < configured tolerance` and rejects anything above it as a real missing
trade (which earlier steps should have caught).

```sql
WITH scope AS (
    SELECT DISTINCT t.ClientAccount AS Account, t.Custody
    FROM Portfolio.v_AccountTransaction t
    WHERE t.Date >= DATEADD(day, -7, CAST(GETDATE() AS date))       -- <period>
),
cust AS (
    SELECT cp.Account, cp.Custody, cp.Date, cp.Asset,
           SUM(cp.Quantity) AS QtyCustody
    FROM Portfolio.v_CustodyPosition cp
    JOIN scope s ON s.Account = cp.Account AND s.Custody = cp.Custody
    WHERE cp.Date >= DATEADD(day, -7, CAST(GETDATE() AS date))      -- <period>
      AND cp.Asset IS NOT NULL
    GROUP BY cp.Account, cp.Custody, cp.Date, cp.Asset
),
ours AS (
    SELECT ap.Account, ap.Custody, ap.Date, ap.Asset,
           SUM(ap.QuantityClose) AS QtyOurs
    FROM Portfolio.v_AccountPosition ap
    JOIN scope s ON s.Account = ap.Account AND s.Custody = ap.Custody
    WHERE ap.Date >= DATEADD(day, -7, CAST(GETDATE() AS date))      -- <period>
    GROUP BY ap.Account, ap.Custody, ap.Date, ap.Asset
)
deltas AS (
    SELECT
        ISNULL(c.Account, o.Account) AS Account,
        ISNULL(c.Custody, o.Custody) AS Custody,
        ISNULL(c.Date,    o.Date)    AS Date,
        ISNULL(c.Asset,   o.Asset)   AS Asset,
        ISNULL(c.QtyCustody, 0) - ISNULL(o.QtyOurs, 0) AS Delta
    FROM cust c
    FULL OUTER JOIN ours o
                 ON o.Account = c.Account AND o.Custody = c.Custody
                AND o.Date    = c.Date    AND o.Asset   = c.Asset
    WHERE ABS(ISNULL(c.QtyCustody, 0) - ISNULL(o.QtyOurs, 0)) > 0
)
SELECT
    d.Account, d.Custody, d.Asset,
    CASE
        WHEN d.Asset IN (SELECT Currency FROM Global.v_Currency) THEN 'cash'
        WHEN a.AssetGroup = 'Cash'                                THEN 'cash'
        ELSE                                                           'non-cash'
    END                            AS Kind,
    COUNT(*)                       AS DateCount,
    MIN(d.Delta)                   AS MinDelta,
    MAX(d.Delta)                   AS MaxDelta,
    AVG(ABS(d.Delta))              AS AvgAbsDelta,
    MAX(ABS(d.Delta))              AS MaxAbsDelta
FROM deltas d
LEFT JOIN Global.v_Asset a ON a.Asset = d.Asset
GROUP BY d.Account, d.Custody, d.Asset, a.AssetGroup
ORDER BY MaxAbsDelta DESC;
```

> **Detector is aggregated per `(Account, Custody, Asset)`** so the audit
> report stays readable on accounts with long histories (a raw per-`(Date,
> Asset)` dump on a 4-month audit can exceed the MCP's output cap). The
> sibling `position-quantity-adjustment` re-executes the underlying per-date
> query on the scope you hand it — that's where the tolerance filter and
> the per-row fix live.

> **`Kind` classification** uses `Global.v_Currency` as the source of truth
> for cash assets (`BRL`, `USD`, `EUR`, `GBP`, `CHF`, …) with a fallback to
> `Global.v_Asset.AssetGroup = 'Cash'`. Do NOT use `AssetGroup = 'Currency'`
> — that value does not exist in production; the AssetGroup for cash is
> literally `'Cash'`, and the cleanest classifier is the currency-code
> membership check.

**Invocation.** `Skill(account-transaction:position-quantity-adjustment)`
with the scoped account list + date window. The sibling runs its own
tolerance check and inserts the right shape per asset kind
(`Status='UPDATED'`, `Obs='automatic quantity adjustment'` for non-cash /
`'automatic cash adjustment'` for cash) — lock-aware (adjustments on/at the
`CheckedDate` are rejected).

**Re-probe.** Same query. Residuals split into:
- **Above tolerance** — real missing trade the previous five steps didn't
  catch. Analyst review (the audit report's `MaxAbsDelta` per asset is the
  fastest way to spot these — anything orders of magnitude above the
  sibling's tolerance is a structural mismatch, not dust).
- **`lock-blocked`** — the sibling declined because the target date is
  on/before the account's active `CheckedDate`. Analyst decision on whether
  to move the lock (out of scope for this skill).

---

## Adding a new step or check

**A new Check 2 step** (a new sibling skill to slot into the sequence) needs:

1. **Where in the order.** Justify the slot: does it unblock a later step
   (goes earlier) or clean up after them (goes later)? Update the "Ordering
   rationale" table.
2. **Detector probe** — one `execute_select_query` scoped to the audited
   window that returns the sibling's candidate pk list. Superset is fine
   (the sibling filters); false negatives are not (the audit stops surfacing
   real work).
3. **Invocation** — the exact `Skill(<plugin>:<name>)` call and what the
   sibling expects as scoping input (pk list, `(Account, Custody, Date)`
   scope, etc.).
4. **Re-probe** — usually the same detector query; document any expected
   post-fix shape change (e.g. compromissada legs no longer match the
   heuristic once normalised).

**A new Check 1 sub-detector** (a new onboarding-pipeline gap) needs the
same shape but with the fix hand-off pointing at an `asset` or `routines`
sibling instead.

Keep detector queries scoped by the same `Date` window the audit was invoked
with (do not silently widen). If a step has legitimate reasons to look
further back (e.g. `1d` scans the unbounded per-asset tape history to know
the earliest date needing prices), make that explicit in the step's body and
name it — do not surprise the analyst.

## Critical rules

- **Executing orchestrator, writes via siblings.** The audit invokes siblings
  via the `Skill` tool. It does NOT call `execute_procedure` or
  `execute_batch(dry_run=false)` directly. Every write happens inside a
  sibling's own preview/confirm/execute cycle.
- **CheckedDate is the ONE hard invariant.** Never call
  `execute_checked_date` from this skill. Each sibling enforces the lock on
  its own writes; rows returned `lock-blocked` are surfaced as-is.
- **One step / sub-detector = one detector SELECT the analyst can rerun.**
  Emit paste-able SQL, not descriptions of SQL.
- **Echo the resolved period + filters** at the top of every run so the
  analyst can trust the scope.
- **Aggregate the report where the analyst thinks in "assets" or "accounts"**
  (Check 1 sub-detectors). Row-by-row detail is for the Check 2 pk lists.
- **Skip-if-empty.** Never invoke a sibling with zero candidates. Report and
  move on.
- **Do NOT add a second confirm layer on top of the sibling's own confirm.**
  The sibling's preview/dry-run IS the confirmation. The audit only pauses
  between steps for interrupt-ability, not for approval.
- **Re-probe every invocation** and report the delta before moving on. A
  bucket that didn't shrink is a signal, not a silent success.
- **Reply in the user's language** (PT / EN).

## When unsure

- **The user asks for a step / check that doesn't exist yet.** Do not invent
  a query on the fly and pass it off as a standing step. Either (a) point
  them at the existing sibling skill that already covers it (`asset:*`,
  `account-transaction:*`, `routines:*`, `position:*`), or (b) propose adding
  it as a new Check 2 step or Check 1 sub-detector and confirm the shape
  with the user before writing it in.
- **A finding sits on/before an active `CheckedDate`.** The sibling will
  reject the write and mark it `lock-blocked`. Surface those rows in the
  step's report but do **not** move the lock — that triggers the recoil
  cycle and is an analyst decision, out of scope here.
- **The period returns nothing.** Say so explicitly per step. An empty
  audit is a valid result and worth stating — the analyst needs to know
  nothing was skipped.
- **A sibling silently declines everything** (re-probe returns `after >=
  before`). Report the sibling's own low-confidence / lock-blocked / declined
  breakdown before proposing the next step. Do NOT loop and retry the same
  invocation.
