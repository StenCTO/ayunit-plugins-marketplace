# `daily-btg-onshore-routine` — JSON schemas and report template

Every step captures its result as a JSON object whose shape is fixed here. The
orchestrator passes these shapes to Claude as the contract for "capture the
leaf skill's output as JSON". The schemas + markdown template are the **only**
moving pieces downstream tooling (future Slack/email, dashboards) will read —
so changes here require a plugin version bump.

## The whole report file (`<date>_daily_btg_onshore.json`)

```json
{
  "run_meta": {
    "routine": "daily-btg-onshore-routine",
    "routine_version": "0.1.0",
    "run_date": "YYYY-MM-DD",
    "accounts_scope": "all" | ["<account>", "..."],
    "dry_run": false,
    "force": false,
    "max_accounts": null,
    "started_at": "YYYY-MM-DDTHH:MM:SS-03:00",
    "finished_at": "YYYY-MM-DDTHH:MM:SS-03:00",
    "claude_session_id": "<id-if-available>",
    "aggregate": {
      "accounts_total": 0,
      "accounts_clean": 0,
      "accounts_partially_resolved": 0,
      "accounts_unresolved": 0,
      "accounts_regressed": 0,
      "accounts_failed": 0,
      "leaves_total_writes": 0,
      "leaves_total_flags": 0
    },
    "status": "OK" | "OK_WITH_RESIDUALS" | "FAILED"
  },
  "audit_summary": {
    "late_accounts_returned": 0,
    "late_accounts_after_filter": 0,
    "filter_reason": "<accounts filter | max_accounts | already-in-sync>"
  },
  "accounts": [
    { /* one AccountRun per late account — see below */ }
  ]
}
```

`run_meta.status` rules:

- `FAILED` if `aggregate.accounts_failed = aggregate.accounts_total` (nothing
  succeeded), or if pre-flight aborted (empty `accounts` list with a captured
  fatal error).
- `OK_WITH_RESIDUALS` if any account is in `partially_resolved`, `unresolved`,
  `regressed`, or `failed` but at least one account was `clean`.
- `OK` otherwise (every account `clean`).

## `AccountRun` (one per late account)

```json
{
  "account": "<9-digit-zero-padded>",
  "custody": "BTG",
  "last_checked_date": "YYYY-MM-DD",
  "last_custody_position_date": "YYYY-MM-DD",
  "last_account_position_date": "YYYY-MM-DD",
  "audit_metrics": {
    "unmatched_assets": 0,
    "pct_diff_position": 0.0,
    "price_issue_assets": 0,
    "pending_trades": 0,
    "qty_mismatch_assets": 0,
    "ap_total_position": 0.0
  },
  "started_at": "YYYY-MM-DDTHH:MM:SS-03:00",
  "finished_at": "YYYY-MM-DDTHH:MM:SS-03:00",
  "status": "clean" | "partially_resolved" | "unresolved" | "regressed" | "failed",
  "step2_position_diff":     { /* see below */ },
  "step3_leaves":            { /* see below */ },
  "step4_portfolio_creator": { /* see below */ },
  "step5_verify":            { /* see below */ },
  "errors": []
}
```

`errors` is a list of `{step, message, sql_or_call, timestamp}` items. A
non-empty `errors` list with `status != "failed"` means one step raised but a
later one recovered — capture both.

## `step2_position_diff`

```json
{
  "as_of": "YYYY-MM-DD",
  "lines": [
    {
      "asset": "<code | null-if-untranslated>",
      "asset_r": "<raw-custody-code | null>",
      "calc_quantity": 0.0,
      "cust_quantity": 0.0,
      "d_qty": 0.0,
      "calc_value": 0.0,
      "cust_value": 0.0,
      "d_value": 0.0,
      "d_value_taxes": 0.0,
      "cause": "untranslated_asset_r" | "missing_trade" | "extra_trade"
             | "wrong_sign" | "pricing_divergence" | "lending_margin"
             | "stale_price" | "tax_divergence" | "multi_feed"
             | "unclassified",
      "proposed_route": "asset-register" | "pending-revalidate"
                      | "pending-position-repair" | "duplicate-trade-reconcile"
                      | "position-quantity-adjustment" | "report-only",
      "evidence": {
        "custody_pks":          [0],
        "candidate_pending_pks":[0],
        "custody_asset_r":      "<raw>",
        "note":                 "<short human note>"
      }
    }
  ],
  "residual_pending_pks": [0],
  "untranslated_asset_r_list": ["<raw>", "..."]
}
```

## `step3_leaves`

Each key is a leaf name; each value is the leaf's own report envelope. The
orchestrator does not re-shape leaf output — it stores it verbatim so the
schema stays honest across leaf-version bumps.

```json
{
  "pending_revalidate": {
    "status": "ran" | "skipped" | "failed",
    "skip_reason": "<string | null>",
    "leaf_report": { /* opaque — the leaf's own report object */ }
  },
  "assetrelated_fix": {
    "status": "ran" | "skipped" | "failed",
    "skip_reason": "<string | null>",
    "leaf_report": { /* opaque */ }
  },
  "pending_position_repair": {
    "status": "ran" | "skipped" | "failed",
    "skip_reason": "<string | null>",
    "leaf_report": { /* opaque — see the leaf's SKILL.md for its report shape */ }
  },
  "duplicate_trade_reconcile": {
    "status": "ran" | "skipped" | "failed",
    "skip_reason": "<string | null>",
    "leaf_report": { /* opaque */ }
  },
  "position_quantity_adjustment": {
    "status": "ran" | "skipped" | "failed",
    "skip_reason": "<string | null>",
    "leaf_report": { /* opaque */ }
  }
}
```

Legal `skip_reason` values: `"no candidates"`, `"dry-run"`,
`"blocked-by-prior-leaf-error"`, `"low-confidence"`, `"human-confirm-required"`,
`"lock-blocked"`. Free-form strings allowed for edge cases; keep them short.

## `step4_portfolio_creator`

```json
{
  "status": "ran" | "skipped" | "failed" | "timeout",
  "skip_reason": "<string | null>",
  "trigger": {
    "tool": "mcp__ayunit__calculate_portfolio",
    "params": {
      "end_date": "YYYY-MM-DD",
      "client_accounts": ["<9-digit-zero-padded>"],
      "create_after_checked_date": true,
      "run_validation": false,
      "consider_cpr": false
    },
    "started_at":  "YYYY-MM-DDTHH:MM:SS-03:00",
    "finished_at": "YYYY-MM-DDTHH:MM:SS-03:00",
    "returned_status": "pending" | "running" | "completed" | "failed" | null,
    "transport_retries": 0
  },
  "job_poll": {
    "job_id": "<string | null>",
    "poll_attempts": 0,
    "final_status": "completed" | "failed" | "not_found" | null,
    "result": { "load_base": true, "positions": true, "shares": true } | null,
    "error": "<string | null>"
  },
  "db_verify": {
    "queried_at":                    "YYYY-MM-DDTHH:MM:SS-03:00",
    "expected_max_date":             "YYYY-MM-DD",
    "db_max_account_position_date":  "YYYY-MM-DD | null",
    "db_rows_in_window":             0,
    "matches_expected":              true
  },
  "verify_source": "job" | "db" | "both" | "none",
  "error": "<string | null>"
}
```

Legal `skip_reason`: `"dry-run"`, `"no-writes-in-step3"`.

`verify_source` rules:

- `"both"` — job poll confirmed `completed` **and** DB check matched.
  Strongest outcome.
- `"db"` — job poll returned `not_found` (per-worker 404 across all retries)
  or `null`, but the DB check confirmed the recalc landed.
- `"job"` — job poll confirmed `completed` but the DB check was skipped
  (should not happen — the DB check always runs; kept for completeness).
- `"none"` — neither source confirmed. Account status = `"failed"`.

`transport_retries` counts retries of the `calculate_portfolio` call itself
(distinct from `job_poll.poll_attempts`, which counts retries of
`get_portfolio_job`).

## `step5_verify`

```json
{
  "as_of": "YYYY-MM-DD",
  "post_lines": [ /* same shape as step2.lines */ ],
  "resolved":  [{ "asset": "<code>", "pre_d_qty": 0.0, "pre_d_value": 0.0 }],
  "residual":  [{ "asset": "<code>", "d_qty": 0.0, "d_value": 0.0 }],
  "regressed": [{ "asset": "<code>", "pre_d_qty": 0.0, "post_d_qty": 0.0, "pre_d_value": 0.0, "post_d_value": 0.0 }],
  "residual_pending_pks": [0]
}
```

## Markdown report template (`<date>_daily_btg_onshore.md`)

The orchestrator fills placeholders and omits empty sub-sections **except** the
Human-action one (which always renders).

```markdown
# Daily BTG Onshore reconcile — {{run_date}}

**Status:** {{run_meta.status icon-and-label}}
**Scope:** {{accounts_scope}}  ·  **dry_run:** {{dry_run}}  ·  **force:** {{force}}
**Window:** {{started_at}} → {{finished_at}}  ·  **routine v{{routine_version}}**

**Late accounts:** {{audit_summary.late_accounts_returned}} returned ·
{{audit_summary.late_accounts_after_filter}} after filter.

**Aggregate:** {{aggregate.accounts_clean}} clean ·
{{aggregate.accounts_partially_resolved}} partial ·
{{aggregate.accounts_unresolved}} unresolved ·
{{aggregate.accounts_regressed}} regressed ·
{{aggregate.accounts_failed}} failed.

---

## Human action required

{{either "None — all clean. ✅" OR a numbered checklist grouped by account:

### Account {{account}}
1. **<short title>** — <one-sentence summary>
   - Investigate:
     ```sql
     <the §3.1 SELECTs verbatim, filled with account + date>
     ```
   - Next step: <leaf-skill or external skill (e.g. `asset-register`, `checkeddate-update`)>
}}

---

## Per-account results

{{for each AccountRun, one section:}}

### {{account}} — {{status icon-and-label}}

| Metric | Value |
|---|---|
| LastCheckedDate | {{last_checked_date}} |
| LastCustodyPositionDate | {{last_custody_position_date}} |
| Days behind | {{last_custody_position_date - last_checked_date}} |
| Audit PctDiffPosition | {{audit_metrics.pct_diff_position}}% |
| Audit PendingTrades | {{audit_metrics.pending_trades}} |
| Audit QtyMismatchAssets | {{audit_metrics.qty_mismatch_assets}} |
| Audit UnmatchedAssets | {{audit_metrics.unmatched_assets}} |
| Audit PriceIssueAssets | {{audit_metrics.price_issue_assets}} |
| Divergence lines (pre) | {{step2.lines | length}} |
| Divergence lines (post) | {{step5.residual | length}} |
| Resolved | {{step5.resolved | length}} |
| Regressed | {{step5.regressed | length}} |
| PortfolioCreator | {{step4.status}} ({{finished_at - started_at}}) |

{{if step2.lines non-empty, render a table:}}

| Asset | dQty | dValue | Cause | Route |
|---|---:|---:|---|---|
| {{asset}} | {{d_qty}} | {{d_value}} | {{cause}} | {{proposed_route}} |

{{if step5.regressed non-empty, render a table prominently marked ⚠️}}

{{if errors non-empty, render a bullet list}}

---

_Report generated by `daily-btg-onshore-routine` v{{routine_version}} ·
state lock: {{state_lock_path}}_
```

## Versioning

If a leaf skill changes the shape of its `leaf_report` in `step3_leaves`, the
orchestrator does **not** need a version bump (leaf reports are opaque here).
If **this** file's schemas change (new step, renamed field, changed enum),
bump `routines/.claude-plugin/plugin.json` `version` and update the schema
here in the same commit.
