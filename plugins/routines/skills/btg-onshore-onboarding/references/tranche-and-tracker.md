# Tranche discipline + tracker XLSX

The BTG onshore inception backlog is a fleet-scale write (50–200
accounts per cutoff typically). Tranche discipline is the analyst's
supervision layer: the orchestrator runs autonomously *within* a tranche,
but the tranche boundary is where the analyst sees a summary and can
intervene.

Cross-references:
- SKILL.md §Step 2 (tranche loop) — the driver.
- SKILL.md §Step 5 (per-tranche checkpoint report) — where this file's
  tracker sheets get updated.
- [`step-schemas.md`](step-schemas.md) — the JSON shapes that feed the
  tracker.
- [`recovery.md`](recovery.md) — how tranches interact with the state
  ledger on resume.

---

## 1. Ordering the backlog

**Top-down by total custody value on cutoff, descending.** From the
§Step 1 audit query:

```
ORDER BY cp.total_value DESC
```

Rationale: large accounts are (a) higher-risk if seeded wrong (visibility
+ P&L impact), and (b) more likely to surface every asset kind the
recipe handles — so early failures show up early. Small accounts run
faster and cleaner and are safe to batch after.

**Do not re-sort within a tranche.** Once a tranche is picked, its
accounts run in the order they entered the tranche, so the ledger and
tracker read left-to-right in the same order.

---

## 2. Pilot rule

**Every cutoff's first tranche is a pilot** at `pilot_size` (default 3).
The pilot's purpose:

1. Prove the BTG feed shape matches [`btg-feed-shape.md`](btg-feed-shape.md)
   for this cutoff (feed schemas drift).
2. Validate the seed recipe on this cutoff's actual data (accY values,
   provision magnitudes, presence of unusual asset kinds).
3. Confirm the CheckedDate advance succeeds end-to-end (permissions,
   lock-conflict rate).

**After the pilot tranche completes, the orchestrator pauses and shows
the summary to the analyst regardless of standing autonomy.** The
analyst says "continue" (typical) or "stop" (fix something first). Only
then does the run open up to `tranche_size` per tranche.

Skip the pilot **only** when re-running an already-piloted cutoff (i.e.
the ledger shows a prior pilot with `pilot_status = "green_go"`). Set
`pilot_size = 0` in the input to skip.

---

## 3. Per-tranche cadence

After the pilot, every tranche is `tranche_size` accounts (default 20).

Per-tranche actions:

1. Pull the next `tranche_size` accounts off the backlog (in-order).
2. Run §Step 3.1 → §Step 3.8 per account (autonomously; continue-on-
   failure).
3. Run §Step 4 pendings sweep per `clean_locked` account.
4. Write the per-tranche report: `_tranche_<N>.{json,md}`.
5. Update the tracker XLSX (§5 below).
6. Print the tranche summary to chat.
7. Continue to the next tranche autonomously — **unless**:
   - `> 30%` of the tranche is `failed` or `seeded_unreconciled` →
     stop, escalate.
   - The analyst intervenes (e.g. "pause after this tranche").

---

## 4. Tranche summary shape (printed to chat)

Kept to one screen so the analyst can scan quickly:

```
Tranche <N> complete — <accounts_in_tranche> accounts, elapsed <mm:ss>

  ✅ clean_locked            <count>   R$<sum>
  ⚠️  seeded_unreconciled    <count>   R$<sum>   ← human-action
  🚫 excluded                <count>            (reason: <top-reason>)
  🚫 blocked_asset_register  <count>            (reason: <top-reason>)
  🚫 blocked_no_price        <count>
  ❌ failed                  <count>            (see report)

  Pendings sweep across clean_locked accounts:
     revalidate promoted:      <n>
     position-repair applied:  <n>
     duplicates IGNORED:       <n>
     human-action items:       <n>

  Report: <path>
  Tracker: <path>
  Backlog remaining: <n> accounts, R$<sum>
```

If any threshold trips (see §3 step 7), print a hard warning under the
summary and wait for the analyst.

---

## 5. Tracker XLSX schema

Path: `reports/inception/BTG_Inception_Tracker_<cutoff_date>.xlsx`

Created on the first tranche, updated after every tranche. Five sheets:

### 5.1 `Progress Summary` (dashboard)

One row per tranche, plus a totals row.

| Column | Description |
|---|---|
| `Tranche` | 0 = pilot, 1..N = subsequent |
| `AccountsInTranche` | Count |
| `ValueInTranche` | Sum of `run_meta.audit.total_value` |
| `CleanLocked` | Count |
| `SeededUnreconciled` | Count |
| `Excluded` | Count |
| `BlockedAssetRegister` | Count |
| `BlockedNoPrice` | Count |
| `Failed` | Count |
| `LockedValue` | Sum of `ValueClose` locked in this tranche |
| `StartedAt` | ISO |
| `FinishedAt` | ISO |
| `ElapsedSec` | Integer |

### 5.2 `Done Accounts` (append-only)

One row per account that reached any terminal status (`clean_locked`,
`seeded_unreconciled`, `excluded`, `blocked_*`, `failed`).

| Column | Description |
|---|---|
| `Account` | 9-digit |
| `Client` | From `Global.ClientAccount` |
| `TotalValueCustody` | Cutoff `SUM(Value)` from `v_CustodyPosition` |
| `TotalValueSeed` | Sum of seed `ValueClose` (0 if not seeded) |
| `AssetCountCustody` | Distinct assets on custody |
| `AssetCountSeed` | Distinct assets seeded |
| `Status` | Terminal status |
| `LockPk` | `pk_CheckedDateID` (NULL if not locked) |
| `Tranche` | Number |
| `AgentNotes` | Free-text from `account.notes[]` |

### 5.3 `Tranche Notes` (analyst-facing narrative)

Free-form markdown per tranche. Autogenerated skeleton per tranche:

```
## Tranche <N> — <cutoff_date> · <start> → <finish>

**Scope:** <n> accounts, R$<sum>

**Highlights:**
- <first surprise, if any>
- <second surprise, if any>

**Human action items in this tranche:** <n> (see Open Items sheet)
```

Analyst adds context after the run. The autogen skeleton is
regenerated on each write; the analyst's additions live in a distinct
markdown block per tranche that the orchestrator preserves.

### 5.4 `Open Items` (human-action queue)

One row per human-action item across all tranches.

| Column | Description |
|---|---|
| `Account` | 9-digit |
| `Tranche` | Number |
| `Category` | `seeded_unreconciled` \| `blocked_asset_register` \| `blocked_no_price` \| `lock_failed` \| `sweep_escalation` \| ... |
| `Summary` | One-sentence description of what's wrong |
| `EvidenceSql` | SQL the analyst runs to investigate |
| `SuggestedNextSkill` | e.g. `asset-register`, `checkeddate-update`, `pending-position-repair` |
| `AddedAt` | ISO |
| `ResolvedAt` | Blank; analyst updates manually |

### 5.5 `Backlog` (remaining)

One row per account not yet processed. Regenerated on every tranche
write (accounts drop off as they enter `Done Accounts`).

| Column | Description |
|---|---|
| `Account` | 9-digit |
| `Client` | |
| `TotalValue` | Cutoff value |
| `AssetCount` | Distinct assets |
| `RowCount` | Custody row count |
| `ExpectedTranche` | Projection: current-tranche + `ceil(rank / tranche_size)` |

---

## 6. Writing the tracker

Use whatever library your local runtime has (`openpyxl` in Python; the
Ayunit MCP does not provide direct XLSX write today). If neither is
available, **fall back to five CSVs** with the same names inside the
same folder (`BTG_Inception_Tracker_<cutoff_date>_Progress.csv`, etc.) —
losing the workbook grouping but preserving the data.

If both fail, print each sheet's rows to chat under a clear header and
tell the analyst *"tracker write failed; copy the tables into the
XLSX manually at `<path>`"*. Never silently drop the tracker.

---

## 7. When a tranche stops

Whether the stop is threshold-triggered (§3 step 7) or analyst-triggered,
the ledger is up-to-date through the last completed account. On resume:

- The ledger is read, completed accounts are skipped, the backlog re-
  ordered on the remaining accounts.
- The **next tranche number continues from the last** (e.g. if pilot
  was tranche 0 and tranche 1 stopped after 12 of 20 accounts, resume
  writes tranche 2 with the remaining 8 + the next 12 from the backlog,
  or an appropriate split at the analyst's direction).

See [`recovery.md`](recovery.md) for the ledger contract.
