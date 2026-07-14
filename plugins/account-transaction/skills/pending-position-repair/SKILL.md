---
name: pending-position-repair
description: "Use when a PENDING row in Portfolio.AccountTransaction cannot be resolved by the loader's own diagnostic (SystemCheck grammar) because its RawTransaction carries no valid asset identifier — but the AccountPosition ↔ CustodyPosition delta on the same account/date makes the intended (Asset, direction) inferrable. This skill takes a pk and an evidence bundle describing the position delta (which asset disappeared/appeared in custody, quantity/value magnitude, book-side counterpart), ranks candidate (Asset, direction) tuples, computes a confidence score, and — on HIGH confidence with a clean reconciliation — issues Portfolio.AccountTransaction_Update @CMD='U' with the inferred fields (SELECT-first-merge, lock-gated, AgentCheck [PR-POS] tag). On MED / LOW confidence, surfaces the ranked candidates for analyst review and writes nothing. Custody-agnostic. Sibling of pending-revalidate (fixes 3-A/B/C blockers cleared by master-data catch-up); this skill fixes 3-Z-unclassified rows repairable by position-delta evidence. Trigger whenever the user says 'infer the missing trade from custody delta', 'this PENDING has no identifier but I know from position it should be X', 'repair pk N with position evidence', or when daily-btg-onshore-routine hands a pk + evidence bundle off to this skill."
---

# Repair `PENDING` rows using `AccountPosition ↔ CustodyPosition` evidence

You are the writer for a **specific class** of stuck `PENDING` rows: those the
loader could not classify because their `RawTransaction` gave it no usable
asset identifier (empty `AssetCustody`, `CustodyIdentifier`, `Isin`, and
`AnbimaCode`), **but** the position delta on the same account and date makes
the intended trade obvious to an analyst. The archetype:

- Row lands `PENDING`, `Asset = NULL`, no identifier in the raw payload.
- `Portfolio.v_CustodyPosition` shows an asset that used to be held has
  disappeared (or an asset appeared) on the trade date.
- `Portfolio.v_AccountPosition` still shows the asset (or doesn't) — the
  divergence and the stuck `PENDING` describe the same event.

Fixing it requires **inference** — no `SystemCheck` grammar helps. This skill
turns that inference into a reproducible, ranked, confidence-scored procedure
that writes autonomously **only when the evidence is unambiguous**, and
surfaces a candidate list for analyst review when it isn't.

**Custody-agnostic.** The technique works on every custody. Do not fork per
custody — the evidence bundle carries all custody-specific facts. This is the
same discipline `pending-revalidate` follows.

**CheckedDate is the safety net.** This skill never advances `CheckedDate`.
Every write lands *after* the active lock (see §Lock-gate) and remains
provisional until the analyst approves it via the specialist path
(`checkeddate-update` / `execute_checked_date`).

## Inputs

The caller supplies **one pk + one evidence bundle** per invocation. Batching
happens at the orchestrator level (one call per divergence line).

```json
{
  "pk": 123456,
  "evidence": {
    "account": "<9-digit>",
    "custody": "BTG",
    "as_of_date": "YYYY-MM-DD",
    "delta": {
      "asset": "<code>",
      "d_qty": 0.0,
      "d_value": 0.0,
      "direction_hint": "asset_disappeared_from_custody" | "asset_appeared_in_custody" | "both"
    },
    "custody_row": {
      "pk_custody_position_id": 0,
      "asset_r": "<raw>",
      "quantity": 0.0,
      "value": 0.0,
      "price": 0.0,
      "obs": "<string | null>"
    } | null,
    "book_row": {
      "pk_account_position_id": 0,
      "quantity_close_prior": 0.0,
      "quantity_close_current": 0.0
    } | null,
    "peer_context": {
      "asset_held_by_account_before": true,
      "asset_held_by_sibling_accounts": true,
      "avg_price_prior": 0.0
    }
  },
  "dry_run": false
}
```

The evidence bundle is deliberately **thick**. The leaf does not query the
divergence itself — the orchestrator (which already computed it in Step 2)
hands it over. This keeps the leaf testable in isolation with hand-crafted
evidence and keeps the orchestrator responsible for interpreting Step-2.

## Reference resources (read on demand)

| Resource | Read when… |
|---|---|
| [`ayunit://docs/transaction/fixes`](ayunit://docs/transaction/fixes) | **First.** Universal write guardrails (SELECT-first-merge, drop `AccountCurrency` / `AccountFx`, absolute values, preserve `RawTransaction`, `AgentCheck`) and the recipes analogous to this repair pattern. |
| [`ayunit://docs/transaction/procedure`](ayunit://docs/transaction/procedure) · [`types`](ayunit://docs/transaction/types) | `AccountTransaction_Update` params, sign convention, auto-validators, `Status` lifecycle. |
| [`ayunit://docs/checkeddate/usage`](ayunit://docs/checkeddate/usage) | **Before any write.** The lock this skill respects and never advances. |
| [`ayunit://docs/position/reconciliation`](ayunit://docs/position/reconciliation) | Vocabulary for the evidence bundle (dQty / dValue interpretation, divergence causes). |

## Tools you call directly

- `execute_select_query` — read the pk (§Step 1), lock-gate (§Step 3), verify
  (§Step 6). Peer-context sanity (asset held historically, avg price).
- `execute_procedure(procedure='Portfolio.AccountTransaction_Update', cmd='U', …)`
  — the single-row write path. `@CMD='U'` is non-destructive.
- `get_procedure_detail` — confirm `AccountTransaction_Update` params on the
  first run of a session; never guess.
- **No** `execute_batch` — this leaf is single-pk by design; batching happens
  at the orchestrator level (one call per divergence line).

## The repair cycle

### 1 — Read the current state (single pk)

```sql
SELECT pk_AccountTransactionID, Date, SettlementDate, ClientAccount, Broker,
       Custody, TransactionType, GeneralLedgerType, GeneralLedgerDescription,
       Currency, AssetCustody, CustodyIdentifier, Asset, AssetRelated,
       Quantity, Price, PriceExFee, ValueGross, Value, Status,
       CAST(SystemCheck AS varchar(1000)) AS SystemCheck_txt,
       CAST(Obs         AS varchar(500))  AS Obs_txt,
       CAST(RawTransaction AS varchar(max)) AS RawTransaction_txt
FROM Portfolio.v_AccountTransaction
WHERE pk_AccountTransactionID = <pk>;
```

Refuse to proceed if:

- `Status <> 'PENDING'` → report as **Skipped — already resolved**.
- `Asset IS NOT NULL` **and** `Asset` matches `evidence.delta.asset` → not this
  skill; hand back to `pending-revalidate` (a 3-B / 3-C case masquerading).
- `AssetCustody IS NOT NULL` or `CustodyIdentifier IS NOT NULL` **and**
  either resolves in `Portfolio.v_AssetCustody` → not this skill; hand back to
  `pending-revalidate` (a 3-A case with cleared blocker).

### 2 — Generate candidates

Build a list of candidate `(Asset, TransactionType)` tuples from the evidence.
For each candidate, compute:

| Signal | How | Weight |
|---|---|---|
| **Delta asset match** | `candidate.Asset == evidence.delta.asset` | required — no match, no candidate |
| **Direction consistency** | If `direction_hint == asset_disappeared_from_custody` and book still shows the asset → `SELL` / `ASSET DELIVERY`. If `asset_appeared_in_custody` and book doesn't → `BUY` / `ASSET RECEIPT`. | required |
| **Quantity match** | `abs(row.Quantity or 0) ≈ abs(evidence.delta.d_qty)` within 0.5% (tighter than value; quantity is usually exact) | +40 |
| **Value magnitude match** | If `row.Value` populated: `abs(row.Value) ≈ abs(evidence.delta.d_value)` within 2% | +30 |
| **Price sanity** | If `row.Value` and `evidence.delta.d_qty` populated: implied `Price ≈ evidence.custody_row.price` within 1% | +15 |
| **Historical hold** | `peer_context.asset_held_by_account_before` true (asset is not novel to this account) | +10 |
| **Single custody candidate** | Only one asset disappeared/appeared in custody on this date on this account | +5 |

Base score 0, sum weights, cap 100.

Reject candidates whose direction is impossible given the row's shape (e.g. a
row with `Value > 0` cannot be a `SELL` unless the sign convention path applies
— consult `ayunit://docs/transaction/types` and `CLAUDE.md §5` before deciding;
the procedure applies the sign, but the raw shape should still be coherent).

### 3 — Lock-gate

```sql
SELECT Account, Custody, Date, Activated
FROM Portfolio.v_CheckedDate
WHERE Account = '<account>' AND Custody = '<custody>' AND Activated = 1
ORDER BY Date DESC;
```

- **>1 `Activated=1` row for the same `(Account, Custody)`** — proc's scalar
  subquery raises (error 512). **Skip** with `status = "skipped-broken-lock"`.
  Do not fix from here.
- **Row's `Date` or `SettlementDate` ≤ lock date** — skip with
  `status = "lock-blocked"`. Emit a paste-able note for the analyst:
  *"lock currently at `<LockDate>` — advance via `checkeddate-update` before
  retrying this repair"*. **Never** advance the lock from this skill.

### 4 — Confidence gate

Sort candidates by score descending. Confidence tier:

| Tier | Rule | Action |
|---|---|---|
| **HIGH** | top candidate score `≥ 85` **and** margin to 2nd candidate `≥ 25`, **and** `peer_context.asset_held_by_account_before = true` **or** `Historical hold` weight not required (novel asset explicitly acknowledged by evidence.custody_row.pk_custody_position_id) | **write** (proceed to Step 5) |
| **MED** | top candidate score `≥ 60` and not HIGH | **report** — do not write; surface top 3 candidates with scores and the specific signals each triggered |
| **LOW** | top candidate score `< 60` or no candidates | **report** — do not write; surface why (no delta-asset match, direction ambiguous, etc.) |

**Never widen the gate to write on MED or LOW** even if the caller passes a
"force-write" flag. Overwriting a `PENDING` with a wrong asset lands a phantom
trade in `AccountPosition` after the next PortfolioCreator run — the safety
net (`CheckedDate` freeze) helps but doesn't erase the analyst's rework.

### 5 — Build the merged params and write (HIGH only)

For the HIGH-confidence candidate:

1. Start from **every populated column** you read in Step 1.
2. **Drop `AccountCurrency` and `AccountFx`** — the proc rejects the whole
   payload otherwise (generic 400 before SQL runs).
3. **Preserve `RawTransaction`** — pass the full JSON string back intact.
4. **Pass absolute values** for `Quantity` / `Price` / `PriceExFee` / `Value` /
   `ValueGross`. The proc applies the sign per `CLAUDE.md §5`.
5. **Overlay the inferred fields:**
   - `Asset = <candidate.Asset>`
   - `AssetRelated = <candidate.Asset>` (repair pattern: `AssetRelated` mirrors
     `Asset` for BUY/SELL/ASSET RECEIPT/ASSET DELIVERY on a self-referencing
     trade; if the caller supplies a different `AssetRelated` in evidence, use
     that instead).
   - `TransactionType = <candidate.TransactionType>` — only overwrite if the
     row's current `TransactionType` is coherent with the direction; else
     surface as **Reported — transaction type conflict** and do not write.
   - Leave `Quantity`, `Value`, `Price`, `PriceExFee`, `ValueGross` at whatever
     the row already carried — the delta match justified the write, and the
     proc's auto-validators (`CLAUDE.md §8`) fill any gaps.
6. Set `Status = 'UPDATED'`. `UPDATED` counts like `VALIDATED` in the pipeline
   and skips the strict "Price required" check.
7. Set `AgentCheck` — mandatory, this is the audit trail the analyst reads:
   ```
   fix YYYY-MM-DD: PENDING position-repair - inferred Asset=<candidate.Asset>, TransactionType=<type> from custody delta on <account> as-of <date> (dQty=<d_qty>, dValue=<d_value>, score=<n>, evidence.custody_pk=<pk>); Status PENDING->UPDATED [PR-POS]
   ```
   The `[PR-POS]` tag distinguishes this repair path from `pending-revalidate`'s
   `[PR]` tag so the audit's verification query can bucket them separately.

8. Submit `execute_procedure(procedure='Portfolio.AccountTransaction_Update',
   cmd='U', params={…})`. Single row, single procedure call — no batch.

### 6 — Verify

Re-SELECT the pk with the Step-1 query:

- `Status` should now be `UPDATED` (or `VALIDATED` if the proc promoted it).
- `Asset` should equal the candidate's asset.
- `SystemCheck` should either be NULL or no longer contain `missing: Asset`.

If the row is still `PENDING`:

- If `SystemCheck` gained a new blocker (e.g. `missing: Price` because the
  proc's auto-fill couldn't derive it), hand back to `pending-revalidate` with
  a note.
- If the row didn't change at all, this is a **detector bug** — do not retry.
  Surface it so the classifier can be fixed. Common causes: `AccountCurrency`
  / `AccountFx` slipped through (proc rejected the whole payload),
  `TransactionType` mismatch with the direction implied by the sign.

## Report shape

End every run with these buckets so nothing is silently dropped:

| Bucket | Meaning | Disposition |
|---|---|---|
| **Written** | HIGH confidence, `Status = UPDATED`, verified | done |
| **Reported — MED confidence** | top score 60–84; top-3 candidates listed with signals | human review |
| **Reported — LOW confidence** | top score < 60 or no candidates; reason listed | human review or gather more evidence |
| **Reported — transaction type conflict** | row's TransactionType incompatible with direction | analyst fixes type first |
| **Skipped — already resolved** | row no longer `PENDING` at read time | done, no write |
| **Skipped — not this skill** | row belongs to `pending-revalidate` (3-A/B/C, cleared blocker) | hand back to the orchestrator with the correct route |
| **Skipped — broken lock** | `(Account, Custody)` has >1 active `CheckedDate` | out of scope (fix duplicate lock first) |
| **Lock-blocked** | `Date` / `SettlementDate` ≤ active lock date | needs `checkeddate-update` (user-approved); emit paste-able note |
| **Reported — detector bug** | wrote but row didn't move | file against inference; do not retry |

Return a single JSON object per invocation:

```json
{
  "pk": 123456,
  "status": "written" | "reported" | "skipped" | "lock-blocked",
  "bucket": "<one of the buckets above>",
  "confidence": "HIGH" | "MED" | "LOW" | null,
  "candidate_chosen": {
    "asset": "<code>",
    "transaction_type": "<type>",
    "score": 0,
    "signals": {"delta_qty_match": true, "value_match": true, "…": true}
  } | null,
  "candidates_all": [
    { "asset": "<code>", "transaction_type": "<type>", "score": 0 }
  ],
  "verify": {
    "post_status": "UPDATED" | "PENDING" | null,
    "post_asset": "<code | null>",
    "post_system_check_head": "<string | null>"
  } | null,
  "agent_check_written": "<the exact AgentCheck string | null>",
  "errors": []
}
```

## Critical rules

- **Custody-agnostic.** Candidate generation reads only the evidence bundle
  plus master-data lookups. Never fork per custody.
- **Never write on MED / LOW confidence.** No "force" flag overrides this.
  A wrong `Asset` on a promoted row lands a phantom in `AccountPosition`;
  even with `CheckedDate` as the safety net, that's rework for the analyst.
- **Never advance `CheckedDate`.** Lock moves are the analyst's decision via
  `checkeddate-update` / `execute_checked_date`. Emit a paste-able note when
  lock-blocked; never `EXEC` it from here.
- **SELECT-first-merge, always.** `@CMD='U'` overwrites every column from what
  you pass. Omitting a populated field is data loss.
- **Drop `AccountCurrency` and `AccountFx`** on every write — the proc
  computes them.
- **Preserve `RawTransaction`** — it's the original custody payload; the
  analyst re-reads it during review.
- **Pass absolute values** for `Quantity` / `Price` / `PriceExFee` / `Value` /
  `ValueGross`. The proc applies the sign.
- **`AgentCheck` with `[PR-POS]` tag** on every write — the audit differ needs
  to distinguish this repair path from `pending-revalidate`'s `[PR]` tag.
- **Verify after every write.** A row that didn't move is a detector bug, not
  a rerun target.
- **Single pk per invocation.** Orchestrators batch by making N calls, not by
  passing pk lists. This keeps failure isolation clean.
- **Reply in the user's language** (PT / EN) and echo the pk + evidence
  summary.

## When unsure

- **Two candidates tie at HIGH score.** Do **not** pick. Surface both as MED
  regardless of raw score — a tie means the evidence doesn't uniquely
  identify the trade.
- **Evidence.delta.asset is NULL** (custody row untranslated). Bucket
  **Reported — LOW confidence** with reason `custody-asset-untranslated`;
  the analyst must register the asset first (`asset-register`), then the
  orchestrator can re-run this skill.
- **Row's `Value` and evidence.delta.d_value disagree by >20%.** Bucket
  **Reported — MED confidence** even if quantity matches. A large value gap
  usually means the row and the delta are unrelated events that coincided.
- **Row is a GL RECEIPT / GL DELIVERY with `Asset = 'BRL'` (cash side).**
  Not this skill. Cash reconciliation plugs are `position-quantity-adjustment`
  territory. Bucket **Skipped — not this skill**.
- **Row is a come-cotas (Obs `LIKE '%COME COTAS%'`).** Not this skill.
  Follow the come-cotas recipe in `CLAUDE.md §6` / `transaction/fixes`.
- **`RawTransaction` is huge (JP, MS-style).** Preserve it verbatim; the
  column is `nvarchar(max)`.
- **The evidence bundle lacks `peer_context`.** Score the candidate without
  the `Historical hold` weight, but require a stricter margin at Step 4
  (top-to-2nd margin `≥ 40` instead of `≥ 25`) to reach HIGH.
