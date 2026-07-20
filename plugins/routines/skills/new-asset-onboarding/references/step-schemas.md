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
  is non-empty, or if `step6_verify.residual_unexpected` is non-empty.
- `OK_WITH_AMBIGUOUS` if no failures but `step3_register.ambiguous_assets` is
  non-empty (the routine did its job; the residual is by-design human-action).
- `OK` otherwise (including the "nothing to onboard" case).

## `step1_detect`

Four sub-detector lists (mirroring `transaction-workday-audit` Check 1's
sub-detectors 1a / 1b / 1c / 1d) plus a sidebar for the rare Asset-FK-broken
corruption case.

```json
{
  "status": "ran" | "skipped" | "failed",
  "skip_reason": null,
  "period": {
    "start_date": "YYYY-MM-DD",
    "end_date":   "YYYY-MM-DD"
  },
  "custody_filter": [],
  "stage_1a_needs_registration": [
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
  "stage_1b_needs_mapping_only": [
    {
      "Custody":            "BTG",
      "AssetCustody":       "<raw>",
      "CustodyIdentifier":  "<raw>",
      "ResolvedAsset":      "<canonical-code — audit pre-resolved via Cnpj/Isin/BbgCode/… probe>",
      "ResolvedCount":      1,
      "FirstSeen":          "YYYY-MM-DD",
      "LastSeen":           "YYYY-MM-DD",
      "RowCount":           0,
      "Accounts":           0,
      "SamplePk":           0
    }
  ],
  "stage_1c_needs_position_backfill": [
    {
      "Custody":       "BTG",
      "Account":       "<zero-padded>",
      "AssetR":        "<custody-side-raw-code>",
      "IsinR":         "<if present>",
      "AnbimaCodeR":   "<if present>",
      "FirstSeen":     "YYYY-MM-DD",
      "LastSeen":      "YYYY-MM-DD",
      "RowCount":      0,
      "TotalQuantity": 0.0,
      "TotalValue":    0.0
    }
  ],
  "stage_1d_needs_price_backfill": [
    {
      "Asset":                     "<canonical-code>",
      "Description":               "<from Global.Asset>",
      "AssetGroup":                "<Equity|Fund|…>",
      "Currency":                  "BRL",
      "Activated":                 1,
      "EarliestTapeDate":          "YYYY-MM-DD",
      "LatestTapeDate":            "YYYY-MM-DD",
      "TapeRowCount":              0,
      "EarliestPriceDate":         "YYYY-MM-DD",
      "LatestPriceDate":           "YYYY-MM-DD",
      "PriceRowCount":             0,
      "ApproxBusinessDaysExpected": 0,
      "ApproxGapCount":            0,
      "tier":                      "full_backfill" | "recent_gap"
    }
  ],
  "asset_fk_broken": [
    {
      "Asset":     "<orphan-code>",
      "Custody":   "<name>",
      "FirstSeen": "YYYY-MM-DD",
      "LastSeen":  "YYYY-MM-DD",
      "RowCount":  0,
      "SamplePk":  0
    }
  ],
  "pending_pks_at_risk": [ 0 ],
  "errors": []
}
```

`pending_pks_at_risk` is the aggregated set of `pk_AccountTransactionID`
values from rows contributing to `stage_1a_needs_registration` and
`stage_1b_needs_mapping_only` — needed later for `pending-revalidate` scope
after Step 4's mapping insert.

`status = "skipped"` never fires for Step 1 (detection always runs); reserved
for shape symmetry with later steps.

## `step2_classify`

Only 1a tuples pass through `asset-lookup`. 1b / 1c / 1d rows are pre-classified
by the audit itself and routed forward without a lookup call. The corruption
sidebar goes straight to `ambiguous_assets`.

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
  "stage_1b_ready_for_mapping": [
    {
      "Custody":            "BTG",
      "AssetCustody":       "<raw>",
      "CustodyIdentifier":  "<raw>",
      "ResolvedAsset":      "<canonical-code>",
      "SamplePk":           0
    }
  ],
  "stage_1c_ready_for_backfill": [
    {
      "Custody":  "BTG",
      "Account":  "<zero-padded>",
      "AssetR":   "<custody-side-raw-code>",
      "RowCount": 0
    }
  ],
  "stage_1d_ready_for_prices": [
    {
      "Asset":            "<canonical-code>",
      "EarliestTapeDate": "YYYY-MM-DD",
      "tier":             "full_backfill" | "recent_gap"
    }
  ],
  "ambiguous_1b_multi_resolve": [
    {
      "Custody":            "BTG",
      "AssetCustody":       "<raw>",
      "CustodyIdentifier":  "<raw>",
      "ResolvedCount":      2,
      "reason":             "identifier matches multiple Global.Asset rows — analyst must pick"
    }
  ],
  "asset_fk_broken_flagged": [
    {
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

`ambiguous_assets` also carries `step2_classify.ambiguous_1b_multi_resolve`
tagged with `refusal_reason = "ambiguous_1b_multi_resolve"`, and
`step2_classify.asset_fk_broken_flagged` tagged with `refusal_reason =
"asset_fk_broken_corruption"` — so the Azure Blob log surfaces every
human-action item in one list regardless of which detector surfaced it.

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

Assets are tagged by their `source_tag` — where they entered the price-backfill
set — so the report shows the registered / mapped / gap distribution.

```json
{
  "status": "ran" | "skipped" | "failed",
  "skip_reason": null,
  "by_asset": {
    "<Asset-code>": {
      "source_tag":         "newly_registered" | "newly_mapped" | "preexisting_1d_gap",
      "window":             { "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD" },
      "dates_inserted":     0,
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

- `newly_registered` — from `step3_register.br_funds_registered ∪
  step3_register.other_registered`.
- `newly_mapped` — from `stage_1b_ready_for_mapping` that still shows in the
  re-detected 1d after Step 4.
- `preexisting_1d_gap` — from `stage_1d_ready_for_prices` (asset was already
  registered + mapped, only Price coverage was incomplete).

Assets in `known_by_lookup` are included only if they also appear in the
re-detected 1d after Step 4; otherwise skipped (their coverage was populated
by an earlier onboarding).

## `step6_verify`

Re-execution of all four `transaction-workday-audit` Check 1 sub-detectors
(1a, 1b, 1c, 1d) plus the corruption sidebar. Each sub-detector's residuals
are tracked separately so the analyst sees which stage of the pipeline still
has work.

```json
{
  "resolved_1a":           [ { "Custody": "...", "AssetCustody": "...", "CustodyIdentifier": "..." } ],
  "resolved_1b":           [ { "Custody": "...", "AssetCustody": "...", "CustodyIdentifier": "...", "ResolvedAsset": "..." } ],
  "resolved_1c":           [ { "Custody": "...", "Account": "...", "AssetR": "..." } ],
  "resolved_1d":           [ { "Asset": "..." } ],
  "residual_expected":     [ { "sub_detector": "1a|1b|1c|1d|sidebar", "key": "...", "reason": "ambiguous|multi_resolve|asset_fk_broken|leaf_declined|lock_blocked" } ],
  "residual_unexpected":   [ { "sub_detector": "1a|1b|1c|1d", "key": "...", "reason": "<hypothesis>" } ],
  "regressed":             [ { "sub_detector": "1a|1b|1c|1d", "key": "..." } ],
  "residual_pending_null_asset_count": 0
}
```

`residual_expected` covers by-design residuals: `ambiguous_assets` cases the
orchestrator declined to auto-fix (1a peer-refusal, 1b `ResolvedCount > 1`,
sidebar corruption), plus 1c/1d rows a leaf declined (`lock_blocked`,
low-confidence). Anything else is `residual_unexpected` and warrants
investigation.

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

{{if step6.residual_unexpected, prepend a red section:}}

## Unexpected residuals (investigate)

| Custody | AssetCustody | CustodyIdentifier | Hypothesis |
|---|---|---|---|
| ... | ... | ... | ... |

---

## Step 1 — Detect (audit Check 1 sub-detectors)

| Sub-detector | Count | Fix needed |
|---|---|---|
| **1a** — Global.Asset missing (needs registration) | {{step1.stage_1a_needs_registration | length}} | register → map → position → prices |
| **1b** — AssetCustody mapping missing (Global.Asset exists) | {{step1.stage_1b_needs_mapping_only | length}} | map → position → prices |
| **1c** — CustodyPosition Asset=NULL AssetR=&lt;code&gt; | {{step1.stage_1c_needs_position_backfill | length}} | position back-fill only |
| **1d** — AssetData.Price backfill gap | {{step1.stage_1d_needs_price_backfill | length}} | price back-fill only |
| Sidebar — Asset FK broken (corruption) | {{step1.asset_fk_broken | length}} | analyst / master data |
| PENDING pks at risk (from 1a + 1b) | {{step1.pending_pks_at_risk | length}} | — |
| Errors | {{step1.errors | length}} | — |

{{if step1.stage_1a_needs_registration, list as a table sorted by RowCount DESC}}
{{if step1.stage_1b_needs_mapping_only, list as a table showing ResolvedAsset per tuple}}
{{if step1.stage_1c_needs_position_backfill, list per (Custody, Account, AssetR) with RowCount}}
{{if step1.stage_1d_needs_price_backfill, list per Asset sorted by ApproxGapCount DESC, showing (EarliestTapeDate, LatestPriceDate, tier)}}

---

## Step 2 — Classify (asset-lookup on 1a only; 1b/1c/1d pre-classified)

| Bucket | Count | Origin |
|---|---|---|
| Already registered (known_by_lookup) | {{step2.known_by_lookup | length}} | 1a — asset-lookup caught audit false-negative |
| BR fund CNPJ candidates | {{step2.br_fund_candidate | length}} | 1a — will register via `register-br-funds` |
| Other unknown | {{step2.other_unknown | length}} | 1a — will register via peer-analogy |
| Ready for mapping (stage_1b) | {{step2.stage_1b_ready_for_mapping | length}} | 1b — audit already resolved Asset code |
| Ready for position back-fill (stage_1c) | {{step2.stage_1c_ready_for_backfill | length}} | 1c — mapping done, only Update_Missing_Asset |
| Ready for prices (stage_1d) | {{step2.stage_1d_ready_for_prices | length}} | 1d — registered + mapped, only prices missing |
| Ambiguous 1b (multi-resolve) | {{step2.ambiguous_1b_multi_resolve | length}} | 1b — ResolvedCount > 1, analyst must pick |
| Asset FK broken (flagged) | {{step2.asset_fk_broken_flagged | length}} | sidebar — corruption, analyst investigates |

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

| Asset | Source tag | Window | Inserted | Missing | Sources |
|---|---|---|---|---|---|
| {{Asset}} | {{source_tag}} | {{start_date}} → {{end_date}} | {{dates_inserted}} | {{dates_still_missing | length}} | {{source_mix as "MD:n AD:n CP:n"}} |

---

## Step 6 — Verify (re-execution of 1a/1b/1c/1d)

| Sub-detector | Resolved | Residual (expected + unexpected) | Regressed |
|---|---|---|---|
| 1a | {{step6.resolved_1a | length}} | — | — |
| 1b | {{step6.resolved_1b | length}} | — | — |
| 1c | {{step6.resolved_1c | length}} | — | — |
| 1d | {{step6.resolved_1d | length}} | — | — |
| **Totals** | — | {{step6.residual_expected | length}} expected · {{step6.residual_unexpected | length}} UNEXPECTED | {{step6.regressed | length}} |

Also: residual PENDING with Asset IS NULL = {{step6.residual_pending_null_asset_count}}.

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
