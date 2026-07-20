# `new-asset-onboarding` — JSON schemas and report template

Every step of the routine captures its result as a JSON object whose shape
is fixed here. The orchestrator passes these shapes to Claude as the
contract for "capture the leaf skill's output as JSON". The schemas plus
the markdown template are the **only** moving pieces downstream tooling
(Azure Blob consumers, future Slack/email, dashboards) will read — so
changes here require a plugin version bump.

## The whole report file (`<end_date>_new_asset_onboarding.json`)

```json
{
  "run_meta": {
    "routine": "new-asset-onboarding",
    "routine_version": "0.1.0",
    "end_date": "YYYY-MM-DD",
    "window_days": 3,
    "custody_filter": [] ,
    "dry_run": false,
    "force": false,
    "started_at": "YYYY-MM-DDTHH:MM:SS-03:00",
    "finished_at": "YYYY-MM-DDTHH:MM:SS-03:00",
    "claude_session_id": "<id-if-available>",
    "status": "OK" | "OK_WITH_AMBIGUOUS" | "FAILED",
    "errors": []
  },
  "step1_detect":       { /* see step1 schema below */ },
  "step2_classify":     { /* see step2 schema below */ },
  "step3_register":     { /* see step3 schema below */ },
  "step4_map_unblock":  { /* see step4 schema below */ },
  "step5_price_history":{ /* see step5 schema below */ },
  "step6_verify":       { /* see step6 schema below */ },
  "blob_upload":        { /* see blob_upload schema below */ }
}
```

`run_meta.status` rules:

- `FAILED` if any step's `errors` is non-empty, or if `step6_verify.regressed`
  is non-empty, or if `step6_verify.unexpected_residual` is non-empty.
- `OK_WITH_AMBIGUOUS` if no failures but `step3_register.ambiguous_assets` is
  non-empty (the routine did its job; the residual is by-design human-action).
- `OK` otherwise (including the "nothing to onboard" case).

## `step1_detect`

```json
{
  "status": "ran" | "skipped" | "failed",
  "skip_reason": null,
  "period": {
    "start_date": "YYYY-MM-DD",
    "end_date":   "YYYY-MM-DD"
  },
  "custody_filter": [],
  "check_1a_unmapped_tuples": [
    {
      "Custody":            "BTG",
      "AssetCustody":       "<raw>",
      "CustodyIdentifier":  "<raw>",
      "FirstSeen":          "YYYY-MM-DD",
      "LastSeen":           "YYYY-MM-DD",
      "RowCount":           0,
      "Accounts":           0,
      "SamplePk":           0,
      "GeneralLedgerDescriptionSample": "<hint at instrument kind>"
    }
  ],
  "check_1b_asset_not_in_global": [
    {
      "pk":     0,
      "Asset":  "<orphan-code>",
      "Custody":"<name>",
      "Date":   "YYYY-MM-DD",
      "GeneralLedgerDescription": "<raw>"
    }
  ],
  "pending_pks_at_risk": [ 0 ],
  "errors": []
}
```

`status = "skipped"` never fires for Step 1 (the audit always runs); reserved
for shape symmetry with later steps.

## `step2_classify`

```json
{
  "status": "ran" | "skipped" | "failed",
  "skip_reason": "step1 empty" | null,
  "known_by_lookup": [
    {
      "Custody":            "BTG",
      "AssetCustody":       "<raw>",
      "CustodyIdentifier":  "<raw>",
      "resolved_Asset":     "<canonical-code>",
      "lookup_verdict":     "FOUND",
      "resolved_via":       "Global.Asset" | "v_AssetCustody",
      "SamplePk":           0
    }
  ],
  "br_fund_candidate": [
    {
      "Custody":            "BTG",
      "AssetCustody":       "<raw>",
      "CustodyIdentifier":  "<cnpj-shaped>",
      "cnpj_normalized":    "<14-digits>",
      "SamplePk":           0
    }
  ],
  "other_unknown": [
    {
      "Custody":            "BTG",
      "AssetCustody":       "<raw>",
      "CustodyIdentifier":  "<raw>",
      "instrument_hint":    "equity|option|bond|treasury|FII|offshore-fund|null",
      "SamplePk":           0
    }
  ],
  "check_1b_recheck": [
    {
      "pk":     0,
      "Asset":  "<orphan-code>",
      "reason": "Asset code set on trade but no row in Global.Asset — corruption"
    }
  ],
  "errors": []
}
```

## `step3_register`

```json
{
  "status": "ran" | "skipped" | "failed",
  "skip_reason": null,
  "br_funds_registered": [
    {
      "Asset":  "<canonical-code>",
      "Cnpj":   "<14-digits>",
      "source": "register-br-funds",
      "AnbimaCode": "<if returned>",
      "SamplePk": 0
    }
  ],
  "other_registered": [
    {
      "Asset":         "<canonical-code>",
      "identifier":    "<what asset-register indexed on>",
      "security_type": "<from Global.Asset>",
      "peer_sample":   ["<peer-Asset>", "..."],
      "source":        "asset-register",
      "SamplePk":      0
    }
  ],
  "ambiguous_assets": [
    {
      "Custody":            "BTG",
      "AssetCustody":       "<raw>",
      "CustodyIdentifier":  "<raw>",
      "refusal_reason":     "<leaf-returned>",
      "SamplePk":           0,
      "note_for_analyst":   "<one-sentence hint at what needs manual input>"
    }
  ],
  "errors": []
}
```

`ambiguous_assets` also carries the `check_1b_recheck` corruption cases,
tagged with `refusal_reason = "check_1b_corruption"` — this way the Azure
Blob log surfaces every human-action item in one list.

## `step4_map_unblock`

```json
{
  "status": "ran" | "skipped" | "failed",
  "skip_reason": null,
  "mappings_inserted": {
    "count": 0,
    "list": [
      { "Asset": "<code>", "Custody": "<name>", "TickerCustody": "<raw>", "TickerCustody2": null }
    ]
  },
  "custody_position_backfilled": 0,
  "pending_revalidate": {
    "unblocked_pks_passed": 0,
    "promoted_to_validated": 0,
    "still_pending": [
      { "pk": 0, "reason": "missing Price" | "missing Quantity" | "<other>" }
    ]
  },
  "errors": []
}
```

## `step5_price_history`

```json
{
  "status": "ran" | "skipped" | "failed",
  "skip_reason": null,
  "by_asset": {
    "<Asset-code>": {
      "window": { "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD" },
      "dates_inserted": 0,
      "dates_still_missing": ["YYYY-MM-DD"],
      "source_mix": {
        "MarketData":      0,
        "AssetDataDB":     0,
        "CustodyPosition": 0
      },
      "errors": []
    }
  },
  "errors": []
}
```

Assets in `known_by_lookup` are **not** included in `by_asset` — their price
history was populated when they were first onboarded (out of this run's
scope).

## `step6_verify`

```json
{
  "resolved":            [ { "Custody": "...", "AssetCustody": "...", "CustodyIdentifier": "..." } ],
  "residual_expected":   [ { "Custody": "...", "AssetCustody": "...", "CustodyIdentifier": "...", "reason": "ambiguous" } ],
  "unexpected_residual": [ { "Custody": "...", "AssetCustody": "...", "CustodyIdentifier": "...", "reason": "<hypothesis>" } ],
  "regressed":           [ { "Custody": "...", "AssetCustody": "...", "CustodyIdentifier": "..." } ],
  "residual_pending_null_asset_count": 0
}
```

## `blob_upload`

```json
{
  "status": "ran" | "skipped" | "failed",
  "skip_reason": "dry_run" | null,
  "uploads": [
    {
      "local_path":     "<absolute path>",
      "blob_name":      "routines/new-asset-onboarding/<end_date>_new_asset_onboarding.<ext>",
      "container_name": "<from tool response or default>",
      "blob_url":       "<returned by tool, if any>",
      "status":         "OK" | "ALREADY_EXISTS" | "FAILED",
      "error":          null
    }
  ]
}
```

## Markdown report template (`<end_date>_new_asset_onboarding.md`)

The orchestrator fills the placeholders, omits empty sub-sections except
the Human-action one (which always renders).

```markdown
# New Asset Onboarding — {{end_date}}

**Status:** {{run_meta.status icon-and-label}}
**Window:** {{step1.period.start_date}} → {{step1.period.end_date}}  ({{run_meta.window_days}} days)
**Custody filter:** {{"ALL" if empty else run_meta.custody_filter | join(", ")}}
**dry_run:** {{dry_run}}  ·  **force:** {{force}}
**Ran:** {{started_at}} → {{finished_at}}  ·  **routine v{{routine_version}}**

---

## Human action required

{{either "None — all clean. ✅" OR a numbered checklist of step3.ambiguous_assets:

1. **<Custody> · <AssetCustody or CustodyIdentifier>** — <refusal_reason>
   - Sample pk: <SamplePk>
   - Hint: <note_for_analyst>
   - Next step: run `asset:asset-register` manually with additional context, OR
     run `asset:asset-enrich-from-bbg` first if a Bloomberg ticker is available.
}}

{{if step6.unexpected_residual, prepend a red section:}}

## Unexpected residuals (investigate)

| Custody | AssetCustody | CustodyIdentifier | Hypothesis |
|---|---|---|---|
| ... | ... | ... | ... |

---

## Step 1 — Detect (transaction-workday-audit)

| Metric | Count |
|---|---|
| Unmapped tuples (Check 1a) | {{step1.check_1a_unmapped_tuples | length}} |
| Asset-not-in-Global (Check 1b) | {{step1.check_1b_asset_not_in_global | length}} |
| PENDING pks at risk | {{step1.pending_pks_at_risk | length}} |
| Errors | {{step1.errors | length}} |

{{if step1.check_1a_unmapped_tuples, list as a table sorted by RowCount DESC}}

---

## Step 2 — Classify (asset-lookup)

| Bucket | Count |
|---|---|
| Already registered (known_by_lookup) | {{step2.known_by_lookup | length}} |
| BR fund CNPJ candidates | {{step2.br_fund_candidate | length}} |
| Other unknown | {{step2.other_unknown | length}} |
| Check 1b re-check | {{step2.check_1b_recheck | length}} |

---

## Step 3 — Register

| Metric | Count |
|---|---|
| BR funds registered | {{step3.br_funds_registered | length}} |
| Other registered (peer-analogy) | {{step3.other_registered | length}} |
| Ambiguous (human-action) | {{step3.ambiguous_assets | length}} |

{{if step3.br_funds_registered, list (Asset, Cnpj, AnbimaCode)}}
{{if step3.other_registered, list (Asset, identifier, security_type)}}

---

## Step 4 — Map + unblock PENDING

| Metric | Count |
|---|---|
| AssetCustody rows inserted | {{step4.mappings_inserted.count}} |
| CustodyPosition rows back-filled | {{step4.custody_position_backfilled}} |
| PENDING promoted to VALIDATED | {{step4.pending_revalidate.promoted_to_validated}} |
| Still PENDING (downstream blocker) | {{step4.pending_revalidate.still_pending | length}} |

---

## Step 5 — Historical prices

| Asset | Window | Inserted | Missing | Sources |
|---|---|---|---|---|
| {{Asset}} | {{start_date}} → {{end_date}} | {{dates_inserted}} | {{dates_still_missing | length}} | {{source_mix as "MD:n AD:n CP:n"}} |

---

## Step 6 — Verify

| Metric | Count |
|---|---|
| Resolved (present in Step 1, gone now) | {{step6.resolved | length}} |
| Residual — expected (ambiguous by design) | {{step6.residual_expected | length}} |
| Residual — unexpected (INVESTIGATE) | {{step6.unexpected_residual | length}} |
| Regressed (appeared after run) | {{step6.regressed | length}} |
| Residual PENDING with Asset IS NULL | {{step6.residual_pending_null_asset_count}} |

---

## Blob upload

| Local file | Blob name | Status |
|---|---|---|
| ... | ... | OK / ALREADY_EXISTS / FAILED |

---

_Report generated by `new-asset-onboarding` v{{routine_version}} ·
state lock: {{state_lock_path}} · run before the daily custody routines._
```

## Versioning

If a leaf skill changes the shape of one of these fields, bump this plugin's
`version` in `.claude-plugin/plugin.json` and update the schema here in the
same commit. The orchestrator's contract with downstream readers (Azure Blob
consumers included) is exactly this file.
