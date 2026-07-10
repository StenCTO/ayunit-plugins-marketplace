---
name: artifact-review
description: "Use when the user asks to review, audit, or critique a candidate HTML Live Artifact at a specific path in this repo — typically a `live-artifacts/*.html` contribution from a colleague or a WIP the user is authoring. Triggers: 'review the artifact at X', 'check this live artifact', 'audit this HTML dashboard', 'valide this artifact against ayunit', 'is this artifact coherent with plugin Y', 'lint this live-artifact'. Reads the HTML/JS, verifies every referenced Ayunit MCP tool exists LIVE, checks Cowork bridge usage (window.cowork.callMcpTool), compares against sibling artifacts in the target plugin for visual/structural consistency, flags security & accessibility hygiene issues, and emits a structured markdown VERDICT (ACCEPT_NEW / MERGE_INTO_SIBLING / REJECT). Does not modify the candidate file — pure review."
---

# artifact-review — audit a candidate live-artifact HTML

Interactive review of a single candidate HTML file (usually under
`plugins/<name>/live-artifacts/`). Same interaction pattern as
`skill-review`: user names the candidate + target scope in the prompt;
this skill runs a fixed checklist and emits a verdict report.

## Inputs (from the user prompt, not CLI)

- **candidate_path** — the HTML file under review, e.g.
  `plugins/anbima-data/live-artifacts/new-dashboard.html`.
- **target_plugin_path** — usually the `plugins/*/` ancestor of the
  candidate.
- **companion_skill_path** (optional) — if the candidate ships alongside a
  new `SKILL.md` (the skill that would emit it), review that too, but
  route the SKILL.md portion to the sibling `skill-review` skill's
  checklist (don't duplicate the logic here).

## Review process (walk in this order)

1. **Read the candidate HTML** end-to-end. Note: title, external
   `<script>`/`<link>` deps (with or without SRI), the JS entry points,
   every `window.cowork.callMcpTool(...)` call site, and any hardcoded
   URLs / data.
2. **Load target-plugin context**: read the plugin `README.md`, each
   sibling artifact under `<target>/live-artifacts/*.html`, and the
   plugin's existing skills. Identify visual/layout tokens the plugin
   already uses (colours, fonts, table/chart patterns) and any documented
   template.
3. **Verify every Ayunit MCP tool referenced by the JS** exists LIVE:
   scan the source for `callMcpTool("mcp__ayunit__X", ...)` calls; every
   `X` must be a real tool in the loaded MCP. For SQL sent through
   `execute_select_query`, sanity-check that named
   tables/views/procedures exist via `mcp__ayunit__list_tables`,
   `list_views`, `list_procedures`, or the corresponding detail tools.
4. **Fit assessment** — new dashboard vs improvement of an existing
   sibling. Compare purpose (data shown, primary user question answered),
   layout, and MCP-tool footprint. Score qualitatively (NONE / LOW /
   MEDIUM / HIGH). If HIGH overlap with a sibling, recommend
   MERGE_INTO_SIBLING and specify what the merged artifact should
   include.
5. **House-convention & hygiene checklist**:
   - **Cowork bridge**: any data-fetch call uses
     `window.cowork.callMcpTool` — no raw `fetch()` to internal Sten
     hosts (would fail in Cowork sandbox), no baked-in bearer tokens.
   - **Self-containment**: no `file://` deps, no references to files
     outside the artifact, no dependence on iframes or externally
     injected globals other than the Cowork bridge itself.
   - **External deps**: `<script src="https://cdn...">` OK, but flag
     missing `integrity=` (SRI) on CDN loads — best-effort, not fatal.
   - **Consistency with siblings** (only when siblings exist): colour
     tokens, typography, header/table/pagination patterns should match.
   - **Security**: no `eval(`, no `new Function(`, no `document.write(`,
     no `innerHTML` receiving unsanitized MCP response strings, no
     hardcoded credentials, no PII in string literals.
   - **Accessibility basics**: semantic HTML (`<button>` not `<div
     onclick>`), tables use `<thead>`/`<tbody>`, form controls have
     labels, images have alt text, colour contrast readable (visual
     judgement from the CSS is enough).
   - **State scoping**: no writes to `localStorage` /
     `sessionStorage` unless the plugin documents that pattern.

## Output — the VERDICT report

Emit a single markdown block, no preamble, same shape as `skill-review`:

```
## VERDICT: <ACCEPT_NEW | MERGE_INTO_SIBLING | REJECT>

### Structural
- [✓/✗/!] one line per rule with the finding (line refs when applicable)

### Ayunit coherence (live-verified)
- ✓ callMcpTool("mcp__ayunit__execute_select_query", …) — tool exists;
      SQL touches AssetDataDB.dbo.FundosAnbima (verified via list_tables)
- ✗ callMcpTool("mcp__ayunit__get_anbima_cri_cra", …) — tool NOT FOUND
      (candidate line N)
- (be exhaustive across every callMcpTool site)

### Fit with target plugin (<plugin-name>)
Sibling artifacts read: artifact-a, artifact-b
- vs artifact-a — overlap: LOW (different data, different purpose)
- vs artifact-b — overlap: HIGH (same data, same primary view) → merge

### Hygiene findings
- ✓ no eval / no new Function
- ! missing SRI hash on https://cdn.jsdelivr.net/npm/chart.js@4.5.0
- ✗ innerHTML receives raw MCP string at line N — sanitize or use
      textContent

### Recommended actions
1. (specific, actionable — one bullet each)
2. …

### Suggested consolidated scope (only if MERGE_INTO_SIBLING)
> …how the merged artifact should differ from artifact-b…
```

## Guardrails

- **Do not modify the candidate file.** Emit findings only.
- **Do not run the HTML.** Review is static — read the source, don't
  attempt to fetch external CDNs or exercise the JS.
- **Live-verify MCP references only.** Don't chase every external URL.
- **Under-report over over-report.** ~40 lines of report; expand only
  when the artifact is complex or the finding list is genuinely long.
- **Cite line numbers** for anything that requires the author to touch a
  specific spot.
