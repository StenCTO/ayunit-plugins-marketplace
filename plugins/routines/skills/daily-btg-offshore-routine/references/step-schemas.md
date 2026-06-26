# `daily-btg-offshore-routine` — JSON schemas and report template

Every step of the routine captures its result as a JSON object whose shape
is fixed here. The orchestrator passes these shapes to Claude as the
contract for "capture the leaf skill's output as JSON". The schemas plus
the markdown template are the **only** moving pieces downstream tooling
(future Slack/email integration, dashboards) will read — so changes here
require a plugin version bump.

## The whole report file (`<date>_daily_btg.json`)

```json
{
  "run_meta": {
    "routine": "daily-btg-offshore-routine",
    "routine_version": "0.1.0",
    "date": "YYYY-MM-DD",
    "accounts_scope": "all" | ["<account>", "..."],
    "dry_run": false,
    "force": false,
    "started_at": "YYYY-MM-DDTHH:MM:SS-03:00",
    "finished_at": "YYYY-MM-DDTHH:MM:SS-03:00",
    "claude_session_id": "<id-if-available>",
    "status": "OK" | "OK_WITH_RESIDUALS" | "FAILED"
  },
  "step1_btg_load":      { /* see step1 schema below */ },
  "step2_assetrelated":  { /* see step2 schema below */ },
  "step3_duplicates":    { /* see step3 schema below */ },
  "verify":              { /* see verify schema below */ }
}
```

`run_meta.status` rules:

- `FAILED` if any of `step1 / step2 / step3.errors` is non-empty.
- `OK_WITH_RESIDUALS` if no errors but
  `verify.residual_pending_count - verify.residual_pending_lock_blocked_count > 0`
  or `step3.suspicious_pairs_flagged > 0` or `step1.unresolved_assets` is
  non-empty.
- `OK` otherwise.

## `step1_btg_load`

```json
{
  "status": "ran" | "failed",
  "accounts_attempted": { "count": 0, "list": ["<account>", "..."] },
  "rows_inserted_validated": 0,
  "rows_inserted_pending": 0,
  "rows_lock_blocked": {
    "count": 0,
    "by_account": { "<account>": 0 }
  },
  "unresolved_assets": [
    { "tradeId": "<id>", "description": "<custody-side-string>", "identifier": "<isin|cusip|ticker|null>" }
  ],
  "accounts_with_zero_activity": ["<account>", "..."],
  "auto_registered_assets": [
    { "asset_code": "<sten-code>", "asset_group": "Bond|FutureCurrency|...", "source": "btg-offshore" }
  ],
  "errors": []
}
```

`status = "failed"` means the leaf skill could not produce a meaningful
result (MCP error, hard parser failure). When `failed`, the count fields
should still be present (set to `0`) and `errors` must carry at least one
string explaining the failure.

## `step2_assetrelated`

```json
{
  "status": "ran" | "skipped" | "failed",
  "skip_reason": "no candidates" | null,
  "candidates": 0,
  "promoted_to_updated": 0,
  "residual_pending": {
    "count": 0,
    "list": [
      { "pk": 0, "asset_guess": "<ticker|null>", "confidence": 0.0, "reason": "<short>" }
    ]
  },
  "low_confidence_skipped": {
    "count": 0,
    "list": [
      { "pk": 0, "description": "<custody-string>", "candidates": ["<ticker>", "..."] }
    ]
  },
  "errors": []
}
```

`status = "skipped"` is normal — most days the BTG load won't leave any
PENDING-INTEREST/DIVIDEND rows.

## `step3_duplicates`

```json
{
  "status": "ran" | "skipped" | "failed",
  "pairs_evaluated": 0,
  "duplicates_removed": {
    "count": 0,
    "list": [{ "pk": 0, "kept_pk": 0, "reason": "<short>" }]
  },
  "suspicious_pairs_flagged": {
    "count": 0,
    "list": [
      { "pk_a": 0, "pk_b": 0, "reason": "<short>", "fields": { "key": "value" } }
    ]
  },
  "errors": []
}
```

## `verify`

```json
{
  "status_counts": {
    "VALIDATED": 0,
    "UPDATED":   0,
    "PENDING":   0,
    "IGNORED":   0
  },
  "residual_pending_count": 0,
  "residual_pending_rows": [
    { "pk": 0, "account": "<account>", "asset": "<asset|null>",
      "description": "<gl-description>", "value": 0.0 }
  ],
  "residual_pending_lock_blocked_count": 0
}
```

## Markdown report template (`<date>_daily_btg.md`)

The orchestrator fills the placeholders, omits empty sub-sections except
the Human-action one (which always renders).

```markdown
# Daily BTG Offshore — {{date}}

**Status:** {{run_meta.status icon-and-label}}
**Scope:** {{accounts_scope}}  ·  **dry_run:** {{dry_run}}  ·  **force:** {{force}}
**Window:** {{started_at}} → {{finished_at}}  ·  **routine v{{routine_version}}**

---

## Human action required

{{either "None — all clean. ✅" OR a numbered checklist:

1. **<short title>** — <one-sentence summary>
   - Investigate:
     ```sql
     <SELECT the analyst should run>
     ```
   - Next step: <leaf-skill or external skill to use>
}}

---

## Step 1 — BTG load

| Metric | Count |
|---|---|
| Accounts attempted | {{step1.accounts_attempted.count}} |
| Validated inserted | {{step1.rows_inserted_validated}} |
| PENDING inserted | {{step1.rows_inserted_pending}} |
| Lock-blocked | {{step1.rows_lock_blocked.count}} |
| Unresolved assets | {{step1.unresolved_assets | length}} |
| Auto-registered assets | {{step1.auto_registered_assets | length}} |
| Errors | {{step1.errors | length}} |

{{if step1.unresolved_assets, list them as a table}}
{{if step1.errors, list them as a bullet list}}

---

## Step 2 — Triage PENDING income

| Metric | Count |
|---|---|
| Status | {{step2.status}} |
| Candidates | {{step2.candidates}} |
| Promoted to UPDATED | {{step2.promoted_to_updated}} |
| Residual PENDING | {{step2.residual_pending.count}} |
| Low-confidence skipped | {{step2.low_confidence_skipped.count}} |

{{if any residual or low-confidence, list pks as a table}}

---

## Step 3 — Duplicate reconcile

| Metric | Count |
|---|---|
| Pairs evaluated | {{step3.pairs_evaluated}} |
| Duplicates removed | {{step3.duplicates_removed.count}} |
| Suspicious pairs flagged | {{step3.suspicious_pairs_flagged.count}} |

{{if suspicious flagged, list (pk_a, pk_b, reason) as a table}}

---

## Verify

| Status | Count |
|---|---|
| VALIDATED | {{verify.status_counts.VALIDATED}} |
| UPDATED   | {{verify.status_counts.UPDATED}} |
| PENDING   | {{verify.status_counts.PENDING}} |
| IGNORED   | {{verify.status_counts.IGNORED}} |

Residual PENDING: **{{verify.residual_pending_count}}** ({{verify.residual_pending_lock_blocked_count}} lock-blocked, expected).

{{if non-lock residuals, list them as a table sorted by |Value| desc}}

---

_Report generated by `daily-btg-offshore-routine` v{{routine_version}} ·
state lock: {{state_lock_path}}_
```

## Versioning

If a leaf skill changes the shape of one of these fields, bump this plugin's
`version` in `.claude-plugin/plugin.json` and update the schema here in the
same commit. The orchestrator's contract with downstream readers is exactly
this file.
