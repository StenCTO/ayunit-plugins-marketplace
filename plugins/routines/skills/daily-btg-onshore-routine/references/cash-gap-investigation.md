# Recipe: Cash-gap investigation

**A material cash gap between `AccountPosition` and `CustodyPosition` is a
diagnostic signal, not a residual.** Every non-trivial deviation on the cash
side (BRL for onshore, USD for offshore) points at exactly one of three root
causes:

1. **Missing trade** — the ledger doesn't record something custody applied.
2. **Wrong trade** — the ledger records an event that doesn't correspond to
   any real cash movement (e.g. a name-change swap misclassified as BUY).
3. **Duplicate cash-side leg** — the same cash effect recorded twice.

The orchestrator **must actively investigate** cash gaps in this order.
Reporting a large BRL delta as "informational — pricing team hand-off" is
almost always wrong — pricing divergences drive **asset-value** gaps
(`Sten Master D5 pricing spread`), not **cash** gaps.

## When to invoke

Run this recipe on every account whose Step-2 diff shows either of:

- `|d_value| > 100` on a cash asset (BRL, USD, etc.), **or**
- `|d_qty| > 0.5` on a cash asset (equivalent — cash is `Price=1`), **or**
- `custody Value IS NULL and book Value <> 0` (custody may not report cash
  on some account types — this is still worth investigating once).

Do **not** trust the audit's `QtyMismatchAssets` here. That signal is
computed by the tool with likely-tuned thresholds and may skip cash.
Verify against the raw diff.

## Step A — Locate the day the gap started (day-by-day walk)

The gap almost always **jumps** on a single day. Walk book vs custody BRL
day-by-day from the last-checked-date forward until the delta changes:

```sql
WITH dates AS (
    SELECT DISTINCT CAST([Date] AS date) AS d
    FROM Portfolio.v_AccountPosition
    WHERE Account   = '<account>'
      AND Custody   = '<custody>'
      AND CAST([Date] AS date) BETWEEN '<LastCheckedDate>' AND '<LastCustodyPositionDate>'
),
ap AS (
    SELECT CAST([Date] AS date) AS d, ValueClose AS ap_value
    FROM Portfolio.v_AccountPosition
    WHERE Account = '<account>' AND Custody = '<custody>' AND Asset = 'BRL'
      AND CAST([Date] AS date) BETWEEN '<LastCheckedDate>' AND '<LastCustodyPositionDate>'
),
cp AS (
    SELECT CAST([Date] AS date) AS d, Value AS cp_value
    FROM Portfolio.v_CustodyPosition
    WHERE Account = '<account>' AND Custody = '<custody>' AND Asset = 'BRL'
      AND CAST([Date] AS date) BETWEEN '<LastCheckedDate>' AND '<LastCustodyPositionDate>'
)
SELECT d.d AS pos_date,
       ap.ap_value AS book_brl,
       cp.cp_value AS cust_brl,
       CAST(COALESCE(ap.ap_value,0) - COALESCE(cp.cp_value,0) AS decimal(18,2)) AS d_brl
FROM dates d
LEFT JOIN ap ON ap.d = d.d
LEFT JOIN cp ON cp.d = d.d
ORDER BY d.d;
```

Read across rows. Find the first `pos_date` where `d_brl` shifts materially
from its prior value. That is the **shock date**.

## Step B — Enumerate the day's transactions

Every `AccountTransaction` on the shock date (`Date` or `SettlementDate`):

```sql
SELECT pk_AccountTransactionID, Date, SettlementDate, Status, TransactionType,
       GeneralLedgerType, GeneralLedgerDescription,
       Asset, AssetRelated, Quantity, Value, ValueGross,
       CAST(Obs AS varchar(300))                        AS Obs_txt,
       CAST(RawTransaction AS varchar(max))             AS RawTransaction_txt
FROM Portfolio.v_AccountTransaction
WHERE ClientAccount = '<account>' AND Custody = '<custody>'
  AND (Date = '<shock_date>' OR SettlementDate = '<shock_date>')
ORDER BY pk_AccountTransactionID;
```

Sum the `Value` field across these rows. The **cash-side sum** for the day
should approximately equal the observed jump in `d_brl`. If the row-value sum
matches the jump magnitude, one or more of those rows is the culprit.

## Step C — Classify the offending row(s)

Cross-reference each row's `RawTransaction.launchType` and
`GeneralLedgerDescription` against the local recipe library:

| Signal | Recipe | Route |
|---|---|---|
| `launchType = 'TROCA DE NOME'` (any TransactionType) | [`troca-de-nome.md`](troca-de-nome.md) | Reclassify to ASSET RECEIPT/DELIVERY |
| Description ~ `'DEPÓSITO DE COTAS E TRIBUTOS PROVISIONADOS'`, `launchType = 'TA'`, `Value = 0` (raw) | [`deposito-tributos-provisionados.md`](deposito-tributos-provisionados.md) | IGNORE (tax-provisioning artifact) |
| Description ~ `'COME COTAS'` | [`come-cotas.md`](come-cotas.md) | UPDATED (recipe fix) |
| Row matches a canonical VALIDATED/UPDATED sibling on same (Date, Asset via AssetRelated, ABS Value) | [`duplicate-first-pass.md`](duplicate-first-pass.md) | IGNORE the newer one |
| GL RECEIPT/DELIVERY without `AssetRelated` (INTEREST/DIVIDEND) | `assetrelated-fix` leaf | Set AssetRelated via description parsing |
| PENDING with `missing: Price` on a deactivated fund | [`deactivated-fund-residue.md`](deactivated-fund-residue.md) | IGNORE |
| None of the above; row shape is plausible but description unrecognised | **Escalate** — report row with paste-able SELECT and defer to analyst |

Apply the recipe autonomously if the signal matches. Otherwise report as
human-action; do **not** invent a fix.

## Step D — Cross-check against BTG's raw feed (when in-DB signal is ambiguous)

If Step C doesn't converge (no recipe fits, description is unfamiliar, or
custody-side data is missing entirely), fall back to BTG's direct feed for
the shock date's month:

**Routing rule** (from `ayunit://docs/feeds/routing`):

- Shock date within the last 60 days → `mcp__ayunit__process_btg_onshore_monthly_transactions`
- Shock date older than 60 days → `mcp__ayunit__process_btg_onshore_transactions_by_period` with `period = "<YYYY-MM>"`

Both tools require `access_name`. The credential profile name for the account
group is registered in `AgnesCredentialsDB` and is not guessable — if the
orchestrator doesn't already know it from prior context, **ask the caller**;
never guess.

The call is **read-only against BTG** (no DB writes). Filter results to
`accounts = [<the account>]` and inspect the returned transactions for the
shock date. Compare row-by-row against what's in `Portfolio.v_AccountTransaction`
on the same date:

- Trades in BTG's feed that are missing from the book → **missing trade** →
  add via `execute_procedure @CMD='I'` after full validation (this is beyond
  the orchestrator's standing recipes; escalate for the initial run of any
  new missing-trade class).
- Trades in the book that BTG doesn't report → **phantom / duplicate / wrong
  trade** → apply the applicable recipe (duplicate-first-pass, troca-de-nome,
  etc.) or escalate.

## Step E — Verify

After applying the fix, re-run **Step A** (the day-by-day walk). The shock
date's `d_brl` should shrink to noise (< R$1). If it doesn't, the recipe was
misapplied or there's a second cause on the same day — investigate again.

## Real-world example (verified 2026-07-15)

Account `005132370`, BTG onshore. Step-2 diff showed
`BRL d_value = -86,010.63`. The orchestrator initially reported this as
"informational — reporting-model difference" and moved on. **Wrong.**

Step A walk revealed the gap was `+R$14,988` (a pre-existing separate issue)
through 2026-07-07, then jumped to `-R$86,010` on 2026-07-08 — a
**−R$101k shock on one day**.

Step B enumerated 3 rows on 2026-07-08:

- pk 96846 BUY 101 CDHY16 @ R$1,000/unit → `Value = -101,000`
- pk 96847 SELL 31 CDHY19 @ R$0.01/unit → `Value = +0.31`
- pk 96848 SELL 70 CDHY22 @ R$0.01/unit → `Value = +0.70`

Row-value sum: -R$100,998.99 ≈ observed jump. **Culprits found.**

Step C classification: all three have `launchType = 'TROCA DE NOME'` in
RawTransaction → apply `troca-de-nome.md` recipe.

Applied: TransactionType BUY→ASSET RECEIPT, SELL→ASSET DELIVERY, Value=0.

Step E verify: BRL delta collapsed from -R$86,010 to +R$0.32 (residual from
the peg-price rounding on the ASSET DELIVERY rows — cosmetic). ✅

## Critical rules

- **Do not report material cash gaps as informational.** Every non-trivial
  cash divergence must be investigated per this recipe.
- **The shock-date walk is mandatory.** Skipping to Step D (BTG feed) without
  first finding the shock date in-DB burns credentials and rate limits for
  no reason.
- **The BTG feed is last resort, not first resort.** Only invoke when Step
  A–C don't converge.
- **Never guess `access_name`.** Ask the caller if not already known.
- **A single-day shock ≠ pricing divergence.** Pricing divergences accrue
  gradually (small daily deltas). A shock is a trade event.
