---
name: transaction-workday-audit
description: "Use when the user wants a daily / workday audit of Portfolio.AccountTransaction — a read-only analyzer that surfaces the recurring defects an analyst is expected to resolve during their routine (unregistered assets in the recent tape, and more checks added over time). Scopes any period (default: last 7 days on Date), runs each enabled check, and returns a per-check report with the offending rows, the recommended hand-off (which sibling skill or master-data action fixes it), and copy-paste-ready SELECTs for verification. This skill NEVER writes to the database — it only reports. Trigger whenever the user says 'audit the tape', 'workday checks', 'daily checks', 'o que precisa resolver hoje', 'quais ativos não estão cadastrados', or asks for a rundown of unresolved items in AccountTransaction for a period."
---

# Workday audit — surface what the analyst must resolve

You are the orchestrator for the analyst's **routine audit** of
`Portfolio.AccountTransaction`. Every workday there is a recurring set of defects
that the loader / validators cannot solve on their own and that a human must
work through: unregistered assets, unattributed income, stuck `PENDING` rows,
broken locks, and so on. This skill is the **single entry point** that scans the
recent tape, runs each enabled check, and hands each finding off to the right
fix skill.

**This skill is read-only.** It runs `execute_select_query` only. It never
issues `I` / `U` / `D`, never moves a CheckedDate, never calls `execute_batch`
with `dry_run=false`. Every finding names the sibling skill or master-data
action that owns the fix.

**This skill is custody-agnostic.** Every check must run uniformly over the
**entire** `Portfolio.v_AccountTransaction` tape and treat every custody the
same way. Do **not** hard-code custody-specific patterns (BTG's virtual→
application fund cycle, XP's `RENDIMENTOS DE CLIENTES` grammar, JP's `Cusip`
field, MS's compromissada format, etc.) into a check's query or its bucketing
logic — those belong in the **custody-specialised sibling skills**
(`duplicate-trade-reconcile`, `assetrelated-fix`, `compromissada-fix`, …). If a
symptom is only meaningful on one custody, it does not belong in this audit;
raise it in the specialist skill. The audit's job is to find *any* row of a
generic shape (unmapped identifier, structural duplicate, stuck PENDING, …)
regardless of who booked it, and hand it to the specialist for interpretation.
A user-supplied `Custody` filter is a scoping choice at call time, never a
built-in assumption of a check.

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

## Tools you call directly

- `execute_select_query` — every read. This is the only DB tool this skill uses.
- `get_view_detail` / `list_views` — confirm columns before writing a new check.
- **No** `execute_procedure`, **no** `execute_batch` — hand off to sibling skills
  for any write.

## The audit cycle

1. **Resolve the period + filters.** Echo them back (`Date` window, account /
   custody scope, checks enabled).
2. **Run each enabled check** with a single `execute_select_query`. Keep each
   query self-contained and paste-able so the user can re-run it.
3. **For each check**, report:
   - **Count** and a compact table of the offending rows (or aggregates when
     the finding is by asset / by account, not by row).
   - **Root cause** in one sentence.
   - **Hand-off** — the sibling skill or master-data action that fixes it, and
     any prerequisite (e.g. `asset-register` must run first before the loader
     will re-map the tape).
   - **Verification SELECT** the user can run after the fix to confirm the
     bucket empties.
4. **End with a summary** — one line per check with its count and hand-off, so
   the analyst has a work list.

---

## Check 1 — Unregistered assets on the recent tape

**Symptom.** A row lands in `Portfolio.v_AccountTransaction` naming an asset the
system does not know how to identify. Two sub-shapes, both surfaced:

**1a — Custody mapping missing (`Portfolio.AssetCustody`).** The loader received
`AssetCustody` and/or `CustodyIdentifier` from the custody feed but found no
mapping for `(Custody, AssetCustody | CustodyIdentifier)` in
`Portfolio.v_AssetCustody`, so `Asset` was left `NULL` and `Status = 'PENDING'`.
The row **does not reach `AccountPosition`** until the mapping (and, if needed,
the underlying `Global.Asset`) is registered and the loader re-processes it.
This is the **common** case and is what "unregistered asset" almost always
means in practice.

**1b — Asset FK not resolving in `Global.Asset`.** The row carries an `Asset`
value that has no matching row in `Global.v_Asset` — a stale/broken FK or a
manually-typed code. Very rare (usually indicates data corruption or a wrongly
edited row); still worth catching.

### Query — 1a: unmapped `(Custody, AssetCustody, CustodyIdentifier)` tuples

Aggregate by identifier so the analyst sees *one row per unregistered asset*,
not one row per trade. Restrict to transaction types that carry an asset (BUY /
SELL / ASSET RECEIPT / ASSET DELIVERY) — GL / DEPOSIT / WITHDRAW rows carry
`Asset='BRL'` (or the account currency) by design and are not "assets" in this
sense.

```sql
SELECT
    t.Custody,
    t.AssetCustody,
    t.CustodyIdentifier,
    MIN(t.Date)               AS FirstSeen,
    MAX(t.Date)               AS LastSeen,
    COUNT(*)                  AS RowCount,
    COUNT(DISTINCT t.ClientAccount) AS Accounts,
    MIN(t.pk_AccountTransactionID)  AS SamplePk
FROM Portfolio.v_AccountTransaction t
LEFT JOIN Portfolio.v_AssetCustody ac
       ON ac.Custody = t.Custody
      AND (ac.TickerCustody  = t.AssetCustody
        OR ac.TickerCustody2 = t.AssetCustody
        OR ac.TickerCustody  = t.CustodyIdentifier
        OR ac.TickerCustody2 = t.CustodyIdentifier)
WHERE t.Date >= DATEADD(day, -7, CAST(GETDATE() AS date))       -- <period>
  AND t.TransactionType IN ('BUY','SELL','ASSET RECEIPT','ASSET DELIVERY')
  AND t.Asset IS NULL
  AND ac.Asset IS NULL                                          -- no mapping exists
  AND (t.AssetCustody IS NOT NULL OR t.CustodyIdentifier IS NOT NULL)
GROUP BY t.Custody, t.AssetCustody, t.CustodyIdentifier
ORDER BY t.Custody, LastSeen DESC;
```

> **The join above is deliberately book-wide.** It tests both
> `AssetCustody` and `CustodyIdentifier` against both `TickerCustody` and
> `TickerCustody2` so the same query works uniformly on every custody without
> per-custody branching. Confirm the view's columns with
> `get_view_detail(view='Portfolio.v_AssetCustody')` before extending, and if
> a new identifier column is added, widen this `ON` clause rather than
> forking the check per custody. A slightly noisier match is preferable to a
> silent false-negative on any single feed.

### Query — 1b: `Asset` set but not in `Global.Asset`

```sql
SELECT DISTINCT
    t.Asset,
    t.Custody,
    MIN(t.Date)  AS FirstSeen,
    MAX(t.Date)  AS LastSeen,
    COUNT(*)     AS RowCount,
    MIN(t.pk_AccountTransactionID) AS SamplePk
FROM Portfolio.v_AccountTransaction t
LEFT JOIN Global.v_Asset a ON a.Asset = t.Asset
WHERE t.Date >= DATEADD(day, -7, CAST(GETDATE() AS date))       -- <period>
  AND t.Asset IS NOT NULL
  AND t.Asset <> 'BRL'
  AND a.Asset IS NULL
GROUP BY t.Asset, t.Custody
ORDER BY LastSeen DESC;
```

### Report shape

Show 1a and 1b in **separate tables** — they have different hand-offs.

For 1a, include a suggested `asset-register` payload hint per row: the raw
`AssetCustody` / `CustodyIdentifier` the analyst will pass into the register
flow, plus the `SamplePk` so they can jump straight to a representative row and
see the `RawTransaction` (Bloomberg-side enrichment often needs the ISIN /
CUSIP that only lives in `RawTransaction`).

### Root cause + hand-off

- **1a (unmapped custody identifier).** New security the loader has never seen
  on this custody, OR the security is registered in `Global.Asset` but not yet
  mapped in `Portfolio.AssetCustody` for this custody. **Hand-off:** the
  `asset-register` skill (and, for BBG-enriched Bloomberg identifiers,
  `asset-enrich-from-bbg` first). After the register + map, hand the affected
  `PENDING` pks to `pending-revalidate` — the loader does **not** retroactively
  re-process rows when master data catches up, so previously-stuck rows stay
  `PENDING` until re-validated.
- **1b (Asset FK not in Global.Asset).** Master-data breakage. **Hand-off:**
  master data owner; do not attempt to auto-fix from this skill.

### Verification

Re-run the two SELECTs after the hand-off completes and confirm the buckets are
empty (or shrunk to only the rows the analyst deliberately deferred).

---

## Check 2 — Potential duplicated trades on the recent tape

**Symptom.** Any feed can book the **same economic event more than once** on
the same account — a plain double load, a feed re-sent on a later day with a
revised NAV / price, or a broker-specific restatement cycle. Any surviving
`VALIDATED` / `UPDATED` duplicate **double-counts** in `AccountPosition`;
`PENDING` duplicates are noise but still worth flagging so the analyst decides
whether they'll clear on the next loader pass or need intervention. This check
is **custody-agnostic**: it scans every custody the same way, on structural
similarity alone.

The audit does **not** decide what's a duplicate — the ground truth is the
custody position and belongs to `duplicate-trade-reconcile`. This check
surfaces **candidate clusters** on structural similarity so the analyst knows
where to point that skill.

### Cluster key

Two rows are a candidate duplicate pair when they share **all** of:

- `ClientAccount`, `Custody`, `Asset`
- `TransactionType`
- `Date` equal (same trade date). A ±1-day widening is deliberately **not**
  built in: some custodies book the two legs of a restatement on consecutive
  dates, but adding that window book-wide inflates false positives on
  high-volume accounts on the custodies that don't. Any consecutive-day
  restatement pattern belongs in `duplicate-trade-reconcile`, which reconciles
  against `Portfolio.CustodyPosition` and can resolve the extra day of
  ambiguity from ground truth.
- `ABS(Quantity)` equal

`Value` / `Price` are **not** in the cluster key by design — a re-sent trade
with a revised NAV keeps the same quantity but shifts the price, and would
otherwise slip through. Instead, `Value` variance inside the cluster is
reported as a signal (identical values → likely plain double load; drifted
values → likely price restatement).

### Query — same-date clusters

```sql
WITH clusters AS (
    SELECT
        t.ClientAccount, t.Custody, t.Asset, t.TransactionType, t.Date,
        ABS(t.Quantity) AS AbsQty,
        COUNT(*)                          AS RowCount,
        COUNT(DISTINCT t.Status)          AS DistinctStatus,
        MIN(t.Value)                      AS MinValue,
        MAX(t.Value)                      AS MaxValue,
        SUM(CASE WHEN t.Status IN ('VALIDATED','UPDATED') THEN 1 ELSE 0 END) AS ValidatedCount,
        SUM(CASE WHEN t.Status = 'PENDING' THEN 1 ELSE 0 END)                AS PendingCount,
        STRING_AGG(CAST(t.pk_AccountTransactionID AS varchar(32)), ',')      AS Pks
    FROM Portfolio.v_AccountTransaction t
    WHERE t.Date >= DATEADD(day, -7, CAST(GETDATE() AS date))    -- <period>
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
        WHEN MinValue = MaxValue                 THEN 'identical-value  (plain double load)'
        WHEN MinValue <> 0 AND ABS(MaxValue - MinValue) / ABS(MinValue) < 0.02
                                                  THEN 'near-identical   (double load w/ minor fx / rounding)'
        ELSE                                          'drifted-value    (likely price restatement)'
    END AS ValueSignal,
    Pks
FROM clusters
ORDER BY Custody, ClientAccount, Date DESC;
```

### Report shape

One cluster per row (already aggregated). Split the report into three buckets
by severity so the analyst knows what to touch first:

| Bucket | Rule | Meaning |
|---|---|---|
| **A — Position-impacting** | `ValidatedCount >= 2` | Two or more `VALIDATED`/`UPDATED` rows in the cluster → `AccountPosition` is already double-counting. Highest priority. |
| **B — Mixed** | `ValidatedCount >= 1 AND PendingCount >= 1` | One counted, one not. Often the `PENDING` is the survivor after a manual triage — check whether it should be `IGNORED` or promoted, or whether the `VALIDATED` twin is the actual duplicate. |
| **C — Pending-only** | `ValidatedCount = 0 AND PendingCount >= 2` | Noise (does not touch `AccountPosition` yet) but will bite if any of them gets promoted. Note; usually clears on the next loader pass. |

For each row show: cluster key (`Account`, `Custody`, `Asset`,
`TransactionType`, `Date`, `AbsQty`), `RowCount`, the bucket, `ValueSignal`,
and the comma-separated `Pks` so the analyst can jump straight to the rows in
`Portfolio.v_AccountTransaction`.

### Root cause + hand-off

- **Bucket A / B / C.** The audit does not reconcile — it points. **Hand-off:**
  `duplicate-trade-reconcile` — pass it the cluster's `Pks` (or the
  `(Account, Custody, Asset, Date)` scope) and it will reconcile against
  `Portfolio.CustodyPosition` and delete the duplicates it can prove.
- **Value-signal `drifted-value`.** Strong hint at a price / NAV restatement
  (the two rows describe the same trade at different prices). Pass the signal
  through in the hand-off note so `duplicate-trade-reconcile` — which knows the
  per-custody restatement grammars — picks the right survivor policy.

### Verification

Re-run the cluster query after `duplicate-trade-reconcile` completes and
confirm the target cluster is gone (or reduced to `RowCount = 1`). A cluster
that persists means the reconcile skill flagged it for a human — check its
report bucket.

---

## Check 3 — `PENDING` rows whose blockers have cleared

**Symptom.** A row landed `PENDING` at load time because a prerequisite wasn't
met — the asset wasn't registered, its custody mapping was missing, no price
was available for the trade date, quantity was zero, etc. **The loader does
not retroactively re-process the tape** when master data catches up: those
rows stay `PENDING` forever unless something re-triggers the validators. So a
`PENDING` row that was legitimately blocked yesterday can be perfectly
resolvable today and no one notices, and the asset silently misses the trade
in `AccountPosition`.

The 91181-91184 pattern (BTG Cayman option trades — `NDX PUT …`, `SPX_INT_B
PUT …`) is the archetype: the asset numeric IDs weren't mapped in
`Portfolio.AssetCustody` at load time, they are mapped **now**, but the rows
are still sitting `PENDING`.

### The loader tells you why — read `SystemCheck` first

The loader writes a **structured diagnostic** into `SystemCheck` on every row
it can't fully validate. The grammar (verified on production):

```
[…partial fixes it applied…]; Revalidation: Status remains PENDING (missing: <field-list>)
```

`<field-list>` is one or more of: `Asset`, `AssetRelated`, `Price`, `Value`,
`Quantity`, `TransactionType`. `SystemCheck` may **also** show that the loader
already resolved some of them (e.g. `Asset identified: 'Sten Master D30' …`),
so a row can be `PENDING` for `Price` even when `Asset` is already filled in.

This is the audit's **classifier**. No custody-specific parsing, no fuzzy
inference — the loader's own words drive the bucketing.

### The five blocker buckets

| Bucket | `SystemCheck` signal | Resolvable-now check | Hand-off |
|---|---|---|---|
| **3-A · Asset unresolved** | `missing: Asset` (often preceded by `Fail to get Asset Register`) | `LEFT JOIN Portfolio.v_AssetCustody` on `(Custody, AssetCustody \| CustodyIdentifier)` — a match today means auto-match will fire on re-validation | `pending-revalidate` |
| **3-B · Price missing** | `missing: Price` | `AssetData.v_Price` has a row for `(Asset, Date)` — the auto-fill validator can compute a Price. Note: the loader's auto-fill only fires when `Price`, `Value`, `ValueGross` are **all** 0/NULL and `Quantity ≠ 0`; if `Value` is populated, `pending-revalidate` will derive `Price = ABS(Value / (Quantity × ContractSize))` from the passed fields instead | `pending-revalidate` |
| **3-C · Quantity missing** | `missing: Quantity` | `Value` and `Price` are populated, so Quantity is derivable — the auto-fill validator will compute it | `pending-revalidate` |
| **3-D · AssetRelated missing** | `missing: AssetRelated` | not this skill's job — the description parser lives in `assetrelated-fix` and knows the three income layouts (A/B/C) | `assetrelated-fix` |
| **3-Z · Special / out of scope** | `Invalid TransactionType`, `MS activity loader: DEBIT CARD`, come-cotas (Obs `LIKE '%COME COTAS%'`), or `SOURCE: FUND_ACCOUNT_STATEMENT` with `missing: Asset, Quantity, Price` (three-way missing → needs manual triage) | not auto-resolvable | reported, no hand-off — analyst decides |

### Query — audit-side detector

Read the tape's `PENDING` scope, parse `SystemCheck` to classify the blocker,
join the resolvability probe for A/B/C. Return one row per PENDING candidate
with its bucket and its "resolvable-now" flag.

```sql
WITH pending AS (
    SELECT
        t.pk_AccountTransactionID, t.Date, t.SettlementDate,
        t.ClientAccount, t.Custody, t.TransactionType,
        t.Asset, t.AssetCustody, t.CustodyIdentifier, t.AssetRelated,
        t.Quantity, t.Price, t.Value, t.ValueGross,
        CAST(t.SystemCheck AS varchar(1000)) AS SystemCheck_txt,
        CAST(t.Obs         AS varchar(500))  AS Obs_txt
    FROM Portfolio.v_AccountTransaction t
    WHERE t.Date >= DATEADD(day, -7, CAST(GETDATE() AS date))     -- <period>
      AND t.Status = 'PENDING'
),
classified AS (
    SELECT p.*,
        CASE
            WHEN p.Obs_txt LIKE '%COME COTAS%'                         THEN '3-Z-come-cotas'
            WHEN p.SystemCheck_txt LIKE '%Invalid TransactionType%'    THEN '3-Z-invalid-txtype'
            WHEN p.SystemCheck_txt LIKE '%DEBIT CARD%'                 THEN '3-Z-debit-card'
            WHEN p.SystemCheck_txt LIKE '%missing: AssetRelated%'      THEN '3-D-assetrelated'
            WHEN p.SystemCheck_txt LIKE '%missing: %Asset%'
             AND p.SystemCheck_txt LIKE '%missing: %Quantity%'
             AND p.SystemCheck_txt LIKE '%missing: %Price%'            THEN '3-Z-three-way-missing'
            WHEN p.SystemCheck_txt LIKE '%missing: %Asset%'            THEN '3-A-asset'
            WHEN p.SystemCheck_txt LIKE '%missing: %Quantity%'         THEN '3-C-quantity'
            WHEN p.SystemCheck_txt LIKE '%missing: %Price%'            THEN '3-B-price'
            WHEN p.SystemCheck_txt IS NULL AND p.Asset IS NULL
             AND (p.AssetCustody IS NOT NULL OR p.CustodyIdentifier IS NOT NULL)
                                                                       THEN '3-A-asset'  -- loader wrote no diagnosis (rare)
            ELSE                                                            '3-Z-unclassified'
        END AS Bucket
    FROM pending p
)
SELECT
    c.Bucket, c.pk_AccountTransactionID, c.Date, c.ClientAccount, c.Custody,
    c.TransactionType, c.Asset, c.AssetCustody, c.CustodyIdentifier,
    c.Quantity, c.Price, c.Value,
    -- resolvable-now probe (A: mapping exists; B: price row exists; C: derivable)
    CASE c.Bucket
        WHEN '3-A-asset'    THEN CASE WHEN ac.Asset IS NOT NULL             THEN 1 ELSE 0 END
        WHEN '3-B-price'    THEN CASE WHEN pr.Asset IS NOT NULL             THEN 1 ELSE 0 END
        WHEN '3-C-quantity' THEN CASE WHEN c.Value IS NOT NULL AND c.Price IS NOT NULL AND c.Price <> 0
                                                                            THEN 1 ELSE 0 END
        ELSE NULL
    END                       AS ResolvableNow,
    ac.Asset                  AS ResolvedAsset,          -- for A
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

> **Bucket-3-A ambiguity check.** When the join returns **more than one**
> `ac.Asset` for the same `(Custody, identifier)` tuple, that's a mapping
> conflict — do **not** flag the row as resolvable-now. Detect with
> `COUNT(DISTINCT ac.Asset) OVER (PARTITION BY c.pk_AccountTransactionID) > 1`
> in the outer SELECT and bump it to `3-Z-ambiguous-mapping`. Rare but real.

### Report shape

Group by bucket. For each row show `pk`, `(Account, Custody, Date)`,
`Bucket`, `ResolvableNow` (1/0), the `ResolvedAsset` (for 3-A), and the head
of `SystemCheck`. Two priority tiers:

- **Ready for `pending-revalidate`** — `Bucket IN (3-A, 3-B, 3-C)` **and**
  `ResolvableNow = 1`. Emit a paste-able pk list per bucket.
- **Blocked / hand-off elsewhere** — `Bucket` = `3-D` (→ `assetrelated-fix`),
  any `3-Z-*` (→ analyst), or `ResolvableNow = 0` (blocker still active —
  will resurface next audit).

### Root cause + hand-off

- **3-A / 3-B / 3-C, resolvable now.** Loader ran before master data caught
  up. **Hand-off:** `pending-revalidate` with the pk list — it re-invokes
  `AccountTransaction_Update @CMD='U'` (SELECT-first-merge, lock-gated,
  atomic batch, verified) so the procedure's auto-validators re-fire.
- **3-A / 3-B / 3-C, not resolvable.** Master data still doesn't have what
  the loader needs. **Hand-off:** `asset-register` (3-A), master-data /
  pricing team (3-B), or the analyst (3-C). Come back to this audit after.
- **3-D.** **Hand-off:** `assetrelated-fix`. Do **not** send to
  `pending-revalidate` — the description parser is not in that skill.
- **3-Z-\*.** Analyst decision — surface `SystemCheck` head and let a human
  read it.

### Verification

Re-run the classifier query after `pending-revalidate` completes. The
targeted pks should no longer appear (their `Status` will be `UPDATED` or
`VALIDATED`). A pk that persists as `PENDING` with the **same** `SystemCheck`
means the re-validation didn't take (the resolvable-now probe was wrong or
the row hit a validator we didn't model) — surface it as a bug against the
detector, not as a rerun.

---

## Adding a new check

Each check is a self-contained section with the same shape:

1. **Symptom** — one paragraph, what the analyst sees.
2. **Query** — one `execute_select_query` the user can copy-paste.
3. **Report shape** — how to summarise the result (per-row vs aggregate).
4. **Root cause + hand-off** — which sibling skill or master-data action fixes
   it (this skill never writes).
5. **Verification** — the SELECT that proves the fix worked.

Keep queries scoped by the same `Date` window the audit was invoked with (do
not silently widen). If a check has legitimate reasons to look further back
(e.g. stuck-PENDING from prior weeks), make that explicit in the section and
name it — do not surprise the analyst.

## Critical rules

- **Read-only.** No `execute_procedure`, no `execute_batch(dry_run=false)`, no
  CheckedDate moves. Every finding is a report and a hand-off.
- **One check = one SELECT the user can rerun.** Emit paste-able SQL, not
  descriptions of SQL.
- **Echo the resolved period + filters** at the top of every report so the
  analyst can trust the scope.
- **Aggregate the report where the analyst thinks in "assets" or "accounts"**
  (unregistered assets, orphan holdings). Row-by-row noise is only useful when
  each row is the unit of work.
- **Name the sibling skill** for every finding. If none exists yet, say so
  explicitly — the analyst will fix it manually and a future skill will
  automate it.
- **Reply in the user's language** (PT / EN).

## When unsure

- **The user asks for a check that doesn't exist yet.** Do not invent a query
  on the fly and pass it off as a standing check. Either (a) point them at the
  sibling skill that already covers it (`assetrelated-fix`,
  `duplicate-trade-reconcile`, `compromissada-fix`, `position-quantity-adjustment`, …),
  or (b) propose adding it as Check N to this skill and confirm the shape with
  the user before writing it in.
- **A finding sits on/before an active CheckedDate.** Flag it (`lock-blocked`)
  but do **not** move the lock — a lock move triggers the recoil cycle and is
  a user decision, out of scope here.
- **The period returns nothing.** Say so explicitly (per check). An empty
  audit is a valid result and worth stating — the analyst needs to know
  nothing was skipped.
