# `btg-onshore-onboarding` — JSON schemas and report template

Every step of the routine captures its result as a JSON object whose
shape is fixed here. The orchestrator passes these shapes to Claude as
the contract for "capture the leaf skill's output as JSON". The schemas
+ the markdown template are the **only** moving pieces downstream
tooling (dashboards, future Slack/email) will read — so changes here
require a plugin version bump.

Cross-references:
- SKILL.md — the routine that produces these objects.
- [`tranche-and-tracker.md`](tranche-and-tracker.md) — the XLSX tracker
  shape that mirrors the JSON.
- [`recovery.md`](recovery.md) — the ledger contract (a compact subset
  of `AccountRun` used to resume mid-flight).

---

## The full report file

Path per tranche: `reports/inception/<cutoff_date>_btg_onshore_onboarding_tranche_<N>.json`
Path final: `reports/inception/<cutoff_date>_btg_onshore_onboarding.json`

```json
{
  "run_meta": {
    "routine": "btg-onshore-onboarding",
    "routine_version": "0.1.0",
    "cutoff_date": "YYYY-MM-DD",
    "custody_name": "BTG",
    "access_name": "<credential-profile-name>",
    "accounts_scope": "all" | ["<account>", "..."],
    "tranche_size": 20,
    "pilot_size": 3,
    "max_accounts": null,
    "dry_run": false,
    "force": false,
    "started_at":  "YYYY-MM-DDTHH:MM:SS-03:00",
    "finished_at": "YYYY-MM-DDTHH:MM:SS-03:00",
    "claude_session_id": "<id-if-available>",
    "aggregate": {
      "accounts_total":                0,
      "accounts_clean_locked":         0,
      "accounts_seeded_unreconciled":  0,
      "accounts_excluded":             0,
      "accounts_blocked_asset_register": 0,
      "accounts_blocked_no_price":     0,
      "accounts_lock_failed":          0,
      "accounts_failed":               0,
      "seed_rows_inserted":            0,
      "locks_advanced":                0,
      "sweep_promoted":                0,
      "sweep_repaired":                0,
      "sweep_ignored":                 0,
      "human_action_items":            0,
      "total_locked_value_brl":        0.0
    },
    "status": "OK" | "OK_WITH_RESIDUALS" | "STOPPED" | "FAILED",
    "errors": []
  },
  "audit_summary": {
    "candidates_returned": 0,
    "candidates_after_exclusions": 0,
    "excluded_pre_tranche": [
      { "account": "<9-digit>", "reason": "<string>" }
    ]
  },
  "tranches": [
    { /* one Tranche per tranche */ }
  ]
}
```

`run_meta.status` rules:

- `FAILED` if `aggregate.accounts_failed = aggregate.accounts_total`
  (nothing succeeded) or if pre-flight aborted.
- `STOPPED` if the tranche loop halted before draining the backlog (a
  tranche tripped the 30%-fail threshold or the analyst stopped it).
- `OK_WITH_RESIDUALS` if any account is not `clean_locked` but at least
  one is.
- `OK` otherwise (every account `clean_locked` and no errors).

## `Tranche`

```json
{
  "tranche_number": 0,
  "kind": "pilot" | "regular" | "resume",
  "accounts_in_tranche": 0,
  "started_at": "…",
  "finished_at": "…",
  "elapsed_sec": 0,
  "checkpoint": {
    "paused_for_analyst": true,
    "analyst_reply": "continue" | "stop" | "<free text>"
  },
  "accounts": [
    { /* one AccountRun per account in this tranche */ }
  ],
  "tranche_summary_printed_to_chat": "<the summary block per tranche-and-tracker.md §4>",
  "tracker_updated": true
}
```

## `AccountRun`

```json
{
  "account": "<9-digit>",
  "client": "<Client name from Global.ClientAccount>",
  "custody": "BTG",
  "audit_metrics": {
    "total_value_custody_brl": 0.0,
    "asset_count_custody":     0,
    "custody_row_count":       0
  },
  "started_at":  "…",
  "finished_at": "…",
  "status": "clean_locked" | "seeded_unreconciled"
          | "excluded" | "blocked_asset_register" | "blocked_no_price"
          | "lock_failed" | "lock_conflict" | "custody_load_lag"
          | "feed_unavailable" | "asset_disambiguation_needed"
          | "seed_build_failed" | "failed",
  "step1_preflight":     { /* schema below */ },
  "step2_feed_pull":     { /* schema below */ },
  "step3_asset_triage":  { /* schema below */ },
  "step4_seed_build":    { /* schema below */ },
  "step5_insert":        { /* schema below */ },
  "step6_reconcile":     { /* schema below */ },
  "step7_lock_advance":  { /* schema below */ },
  "step8_pendings_sweep":{ /* schema below */ },
  "notes": [],
  "errors": []
}
```

## `step1_preflight`

```json
{
  "gate_1a_registered": true,
  "gate_1b_no_view_position": true,
  "gate_1c_no_base_position": true,
  "gate_1d_no_checked_date": true,
  "n_unresolved_at_cutoff": 0,
  "failures": [ { "gate": "1b", "detail": "n=3 view rows exist" } ]
}
```

## `step2_feed_pull`

```json
{
  "tool": "get_btg_onshore_account_information" | "process_btg_onshore_position",
  "reference_date": "YYYY-MM-DD",
  "sections_seen":  ["funds", "fixedIncome", "cash", "..."],
  "sections_unmapped": [],
  "facts_json_path": "<path to facts_<account>_<cutoff>.json>",
  "lot_counts": {
    "funds": 0, "pension": 0, "fi": 0, "coe": 0,
    "treasury": 0, "equity": 0, "repo": 0, "cash": 0
  },
  "warnings": []
}
```

## `step3_asset_triage`

```json
{
  "unresolved_at_start": 0,
  "asset_lookups": [
    {
      "identifier": "<raw>",
      "verdict": "FOUND_GLOBAL_ASSET" | "FOUND_ASSETCUSTODY" | "NOT_FOUND" | "AMBIGUOUS",
      "resolved_asset": "<code | null>",
      "confidence": "HIGH" | "MEDIUM" | "LOW"
    }
  ],
  "assetcustody_fills": {
    "mappings_inserted": 0,
    "custody_position_backfilled": 0,
    "unblocked_pks": []
  },
  "asset_registers": {
    "br_funds_registered": [ { "Asset": "…", "Cnpj": "…" } ],
    "other_registered":    [ { "Asset": "…", "identifier": "…", "security_type": "…" } ],
    "refused":             [ { "identifier": "…", "reason": "…" } ]
  },
  "price_backfills": {
    "assets_probed": 0,
    "assets_backfilled": 0,
    "assets_still_missing_price_on_cutoff": []
  },
  "unresolved_at_end": 0
}
```

## `step4_seed_build`

```json
{
  "rows_built": 0,
  "kind_breakdown": {
    "funds": 0, "pension": 0, "fi": 0, "coe": 0,
    "treasury": 0, "equity": 0, "repo": 0, "cash": 0
  },
  "sanity_gate_failures": [
    { "asset": "…", "gate": "identity_break" | "provision_negative" | "vat_gt_value" | "avgprice_missing" | "fi_match_zero" | "sign_mismatch", "detail": "…" }
  ],
  "notes": [ "accY_invalid on <asset>", "fi_lot_combined on <asset> (n=<k>)", "…" ]
}
```

## `step5_insert`

```json
{
  "canary_pk": 0,
  "canary_verified": true,
  "batch": [
    { "asset": "…", "pk_AccountPositionID": 0, "status": "ok" | "failed", "error": null }
  ],
  "rows_inserted": 0,
  "rows_failed": 0,
  "duplicates_skipped": 0,
  "rollback_applied": false,
  "rollback_pks": []
}
```

## `step6_reconcile`

```json
{
  "asset_count_seed":    0,
  "asset_count_custody": 0,
  "qty_diffs_nonzero_noncash": [
    { "asset": "…", "seed_qty": 0.0, "cust_qty": 0.0, "d_qty": 0.0 }
  ],
  "value_diffs": [
    { "asset": "…", "seed_value": 0.0, "cust_value": 0.0, "d_value": 0.0 }
  ],
  "null_sides": [
    { "asset": "…", "side": "seed" | "custody" }
  ],
  "total_seed_value_brl":    0.0,
  "total_custody_value_brl": 0.0,
  "verdict": "clean" | "value_only_diff" | "qty_diff" | "null_side"
}
```

## `step7_lock_advance`

```json
{
  "attempted": true,
  "reason_if_skipped": null,
  "pk_CheckedDateID": 0,
  "verified_active": true,
  "error": null
}
```

## `step8_pendings_sweep`

```json
{
  "scope_settlement_after": "YYYY-MM-DD",
  "buckets": {
    "A_pending_revalidate": {
      "status": "ran" | "skipped" | "failed",
      "leaf_report": { /* opaque */ },
      "promoted_count": 0,
      "stale_price_flagged":  []
    },
    "B_position_repair": {
      "status": "ran" | "skipped" | "failed",
      "leaf_report": { /* opaque */ },
      "repaired_count": 0,
      "human_action_med_low_pks": []
    },
    "C_duplicate_reconcile": {
      "status": "ran" | "skipped" | "failed",
      "leaf_report": { /* opaque */ },
      "ignored_count":    0,
      "deleted_count":    0,
      "aq_virtual_pairs": 0
    },
    "D_come_cotas": {
      "status": "ran" | "skipped" | "failed",
      "promoted_count": 0
    },
    "E_escalated": [
      { "pk": 0, "reason": "redemption_partial" | "unknown_gl_type" | "…", "summary": "…", "sql_to_investigate": "…" }
    ]
  }
}
```

---

## Markdown report template

Path per tranche: `<cutoff_date>_btg_onshore_onboarding_tranche_<N>.md`
Path final: `<cutoff_date>_btg_onshore_onboarding.md`

Fill placeholders; omit empty sub-sections **except** the Human-action
one (which always renders).

```markdown
# BTG onshore inception — cutoff {{cutoff_date}} — {{tranche_kind_or_final}}

**Status:** {{run_meta.status icon-and-label}}
**Scope:** {{accounts_scope}} · **dry_run:** {{dry_run}} · **force:** {{force}}
**Window:** {{started_at}} → {{finished_at}} · **routine v{{routine_version}}**

**Backlog:** {{audit_summary.candidates_returned}} candidates ·
{{audit_summary.candidates_after_exclusions}} after exclusions.

**Aggregate:**
- ✅ {{aggregate.accounts_clean_locked}} clean-locked (R${{aggregate.total_locked_value_brl}})
- ⚠️ {{aggregate.accounts_seeded_unreconciled}} seeded-unreconciled
- 🚫 {{aggregate.accounts_excluded}} excluded
- 🚫 {{aggregate.accounts_blocked_asset_register}} blocked (asset-register)
- 🚫 {{aggregate.accounts_blocked_no_price}} blocked (no price)
- ⚠️ {{aggregate.accounts_lock_failed}} lock-failed
- ❌ {{aggregate.accounts_failed}} failed
- Sweep: {{aggregate.sweep_promoted}} promoted · {{aggregate.sweep_repaired}} repaired · {{aggregate.sweep_ignored}} duplicates IGNORED

---

## Human action required

{{either "None — all clean. ✅" OR a numbered checklist grouped by account:

### Account {{account}}
1. **<short title>** — <one-sentence summary>
   - Category: <status>
   - Investigate:
     ```sql
     <the SQL to inspect>
     ```
   - Next step: <leaf-skill or external skill (e.g. `asset-register`, `checkeddate-update`, `pending-position-repair`)>
}}

---

## Per-tranche results

{{for each Tranche, one section:}}

### Tranche {{tranche_number}} — {{kind}}

{{summary table + accounts table with per-account status, value, notes}}

---

## Per-account details (expandable)

{{for each AccountRun, one collapsed section with the full detail:}}

<details>
<summary>{{account}} — {{status}}</summary>

- Client: {{client}}
- Cutoff custody value: R${{audit_metrics.total_value_custody_brl}}
- Assets on custody: {{audit_metrics.asset_count_custody}}
- Assets seeded: {{step4_seed_build.rows_built}}
- Locked at: {{step7_lock_advance.pk_CheckedDateID}}
- Notes: {{notes}}
- Errors: {{errors}}

</details>

---

_Report generated by `btg-onshore-onboarding` v{{routine_version}} ·
state lock: {{state_lock_path}}_
```

---

## Versioning

If a leaf skill changes the shape of its `leaf_report` in
`step8_pendings_sweep.buckets`, this orchestrator does **not** need a
version bump (leaf reports are opaque here). If **this** file's schemas
change (new step, renamed field, changed enum), bump
`routines/.claude-plugin/plugin.json` `version` and update the schema
here in the same commit.
