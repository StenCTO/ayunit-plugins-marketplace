# Post-cutoff PENDING sweep patterns

After a BTG onshore account is `clean_locked` at cutoff (§Step 3.8),
the orchestrator sweeps every `Portfolio.AccountTransaction` row loaded
**after the cutoff** to promote what it can and escalate what it can't.
This file catalogues the classification patterns and the routing.

Cross-references:
- SKILL.md §Step 4 (post-cutoff PENDING sweep) — the driver.
- [`account-transaction:pending-revalidate`](../../../../account-transaction/skills/pending-revalidate/SKILL.md)
  — the leaf that re-fires validators on unblocked pks.
- [`account-transaction:pending-position-repair`](../../../../account-transaction/skills/pending-position-repair/SKILL.md)
  — infers `(Asset, direction)` from the AP/CP delta.
- [`account-transaction:duplicate-trade-reconcile`](../../../../account-transaction/skills/duplicate-trade-reconcile/SKILL.md)
  — the two-phase U→IGNORED → verify → D pattern.
- Sibling: `plugins/routines/skills/daily-btg-onshore-routine/references/duplicate-first-pass.md`
  — the daily routine's Mode B account-wide duplicate sweep; the
  same discipline applies here.

---

## 1. Scope of the sweep

All rows matching:

```sql
SELECT pk_AccountTransactionID, Date, SettlementDate, Status,
       TransactionType, GeneralLedgerType, GeneralLedgerDescription,
       Asset, AssetRelated, AssetCustody, CustodyIdentifier,
       Quantity, Price, PriceExFee, Value, ValueGross,
       CAST(Obs AS varchar(300))            AS Obs_txt,
       CAST(RawTransaction AS varchar(max)) AS RawTransaction_txt,
       CAST(SystemCheck AS varchar(1000))   AS SystemCheck_txt
FROM Portfolio.v_AccountTransaction
WHERE ClientAccount = '<account>' AND Custody = '<custody_name>'
  AND CAST(SettlementDate AS date) > '<cutoff_date>'
  AND Status = 'PENDING'
ORDER BY SettlementDate, pk_AccountTransactionID;
```

The `SettlementDate > cutoff` scope is the invariant: anything settling
on or before cutoff is inside the frozen window (protected by the
`CheckedDate`); anything settling after is the daily-routine's future
scope, but at inception we want to clear the low-hanging fruit before
handing off.

**Also sweep `VALIDATED` / `UPDATED` post-cutoff rows for duplicate
clusters** (Mode B pattern) — see §5 below.

---

## 2. Bucket A — asset-mapping cleared by §Step 3.3

The `unblocked_pks` list returned by `asset:assetcustody-fill` in §Step
3.3 identifies pks whose blocker was "no per-custody mapping"
(`v_AssetCustody` was missing the row) — now cleared. Feed these
directly to `pending-revalidate` at the top of the sweep so their
auto-Asset-match validator fires first.

Invocation:

```
Skill('account-transaction:pending-revalidate',
      scope = { account: '<account>', custody: '<custody_name>',
                settlement_after: '<cutoff_date>',
                prepend_pks: [<unblocked_pks from Step 3.3>] })
```

Expected outcome: every pk on `unblocked_pks` promotes PENDING → VALIDATED.

**Verify auto-filled fund-buy prices before trusting them.** The
loader's auto-validator §8 rule fills `Price` from `AssetData.v_Price`
when Price/Value/ValueGross are all NULL AND Quantity ≠ 0. For funds
that have moved between cutoff and today, the auto-filled price is
**stale** relative to the settled quantity. Cross-check every promoted
fund-BUY: the quantity delta on `v_CustodyPosition` between the trade
date and `T+1` should match the promoted `Quantity`. If not, IGNORE
the promotion and escalate (the trade needs a manual Price from BTG).

Stale-price lesson learned via failure — see the daily sibling's
`pending-revalidate` invocation notes.

---

## 3. Bucket B — Asset-NULL with no identifier resolvable

A `PENDING` row with `Asset IS NULL AND (AssetCustody IS NULL OR
CustodyIdentifier IS NULL)` — the loader received no per-lot identifier
BTG could translate.

Route to `account-transaction:pending-position-repair`. The leaf infers
`(Asset, direction)` from the delta between `v_AccountPosition` and
`v_CustodyPosition` on the trade date. HIGH-confidence writes only;
MED/LOW comes back as human-action.

```
Skill('account-transaction:pending-position-repair',
      scope = { pk: <pk>, evidence_bundle: <supplied by orchestrator> })
```

Evidence bundle shape: `{pk, dQty, dValue, candidate_asset, direction,
custody_row, book_row}` — the orchestrator computes these from the
account's day-of-trade AP vs CP diff.

---

## 4. Bucket C — Duplicate trades (BTG re-loads, restatements, AQUISIÇÃO VIRTUAL)

Three sub-patterns:

### 4.1 BTG re-load duplicate

Same economic event loaded twice, days apart, with distinct pks in
different `InputDate` clusters. Detection:

```sql
SELECT CAST(Date AS date) AS dt, TransactionType, Asset,
       ABS(Value) AS abs_val, GeneralLedgerDescription,
       COUNT(*) AS n,
       STRING_AGG(CAST(pk_AccountTransactionID AS varchar), ',') AS pks,
       STRING_AGG(Status, ',') AS statuses,
       MIN(InputDate) AS earliest_input, MAX(InputDate) AS latest_input
FROM Portfolio.v_AccountTransaction
WHERE Custody = '<custody_name>' AND ClientAccount = '<account>'
  AND Status IN ('VALIDATED','UPDATED')
  AND CAST(SettlementDate AS date) > '<cutoff_date>'
GROUP BY CAST(Date AS date), TransactionType, Asset,
         ABS(Value), GeneralLedgerDescription
HAVING COUNT(*) > 1;
```

Real re-load discriminator (per daily-btg-onshore-routine
`duplicate-first-pass.md` Mode B):

- `latest_input - earliest_input` > days (not seconds).
- PK cluster spans across a re-load block (e.g. 48xxx original vs 52xxx
  re-load) rather than sequential.
- `GeneralLedgerDescription` identical.
- Value magnitude identical to the cent.
- **Custody delta corroboration**: day-over-day `CustodyPosition` change
  on the affected asset matches ONE row's impact, not both.

All five signals → route to
`account-transaction:duplicate-trade-reconcile` for the two-phase
U→IGNORED → verify → D pattern. Autonomous.

### 4.2 Restated trade (canonical + PENDING duplicate)

The loader promoted the canonical trade (`VALIDATED`) and left a
duplicate on `PENDING` (usually via a different `GeneralLedgerType`
framing). Detection is `duplicate-first-pass` Mode A shape:

```sql
-- see daily-btg-onshore-routine/references/duplicate-first-pass.md
-- adapted here to `SettlementDate > cutoff` scope
```

Apply the same recipe: IGNORE the PENDING duplicate, canonical
survives. Autonomous.

### 4.3 AQUISIÇÃO VIRTUAL → APLICAÇÃO pair

Per [`btg-identifier-quirks.md`](btg-identifier-quirks.md) §4:

- Detection: two rows on close dates (same day or T+1), same `(Account,
  Custody, Asset via AssetRelated cross-match, ABS(Quantity),
  ABS(Value))`. One has `GeneralLedgerDescription LIKE '%AQUISIÇÃO
  VIRTUAL%'` (or `%AQ VIRTUAL%`); the other `LIKE '%APLICAÇÃO%'`.
- Fix: IGNORE the AQUISIÇÃO VIRTUAL row (it's the provisioning artifact
  from BTG's internal accounting; the APLICAÇÃO row is the settled
  event).
- Route through `account-transaction:duplicate-trade-reconcile` with an
  explicit `preferred_survivor = "APLICAÇÃO"` hint.

---

## 5. Bucket D — Come-cotas on `SettlementDate > cutoff`

If the cutoff is on or before May/November come-cotas dates and the
settlement falls after cutoff, come-cotas SELL rows will be in the
sweep. Route to the come-cotas recipe (same as
`plugins/routines/skills/daily-btg-onshore-routine/references/come-cotas.md`):

- `TransactionType = 'SELL'`, `Value = 0`, `ValueGross = IR amount`,
  `PriceExFee = NAV`, identity `ValueGross ≈ |Quantity| × PriceExFee`.
- Fix: `Status PENDING → UPDATED`, all fields preserved, `[CC]` tag.

Come-cotas rows are **always PENDING** from the loader (the auto-
validator can't derive a coherent Price when `Value = 0`), so they
never surface in the VALIDATED/UPDATED duplicate sweep — only the
PENDING scope catches them.

---

## 6. Bucket E — Redemptions and UNKNOWN cash movements

Any `PENDING` row that doesn't fit A–D:

- Fund redemptions with a partially-filled payload → escalate. The
  redemption's expected `Value` depends on the fund's NAV between
  request and settlement — the orchestrator can't reliably infer.
- `GeneralLedgerType = 'UNKNOWN'` or `'OTHER'` with an unfamiliar
  description → escalate. Don't improvise a `TransactionType` for
  something the loader classified as unknown.

Route to human-action with the row's SQL and one-sentence description.

---

## 7. Sweep ordering (invariant)

Per SKILL.md §Step 4, the sweep runs the leaves in this fixed order:

1. `pending-revalidate` (with `unblocked_pks` prepended)
2. `pending-position-repair` (HIGH-confidence only)
3. `duplicate-trade-reconcile` (Modes A, B, and AQUISIÇÃO VIRTUAL pair)
4. Come-cotas recipe (if any PENDING SELL matches)
5. Escalate everything remaining as human-action

**Ordering non-negotiable.** Running `pending-revalidate` after
`duplicate-trade-reconcile` risks promoting a PENDING duplicate before
the duplicate check sees it — same failure mode as the daily sibling
learned at pk 80501/80502 on account 005132370.

---

## 8. Capture per bucket

For every leaf invocation, capture the leaf's structured `leaf_report`
into `account.step4_sweep.<bucket>` per [`step-schemas.md`](step-schemas.md).
The orchestrator does not re-shape the leaf output.
