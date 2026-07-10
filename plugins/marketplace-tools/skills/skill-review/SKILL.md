---
name: skill-review
description: "Use when the user asks to review, audit, lint, or critique a candidate SKILL.md file at a specific path in this repo — a contribution from a colleague or a WIP the user is authoring themselves. Triggers: 'review the skill at X', 'check this candidate skill against plugin Y', 'audit this SKILL.md', 'valide this skill against ayunit', 'is this a new skill or should it be merged into Z', 'lint this skill'. Reads the candidate, cross-checks every referenced Ayunit MCP tool / procedure / table / view / doc concept LIVE via the Ayunit MCP, compares against sibling skills in the target plugin, and emits a structured markdown VERDICT (ACCEPT_NEW / MERGE_INTO_SIBLING / REJECT) with line-referenced findings. Does not modify the candidate file — pure review."
---

# skill-review — audit a candidate SKILL.md

Interactive review of a single candidate `SKILL.md`. The user names the
candidate path (and often the target plugin) in the prompt; this skill
walks a fixed checklist against that scope and emits a verdict report.

## Inputs (from the user prompt, not CLI)

- **candidate_path** — the file path of the SKILL.md under review, e.g.
  `plugins/anbima-data/skills/new-fund-flow/SKILL.md`.
- **target_plugin_path** (usually derivable) — the parent plugin folder,
  e.g. `plugins/anbima-data/`. Fall back to the candidate's `plugins/*/`
  ancestor if not stated.
- **scope hints** the user may add — "against the ayunit MCP tools and
  docs", "check overlap with existing skills", "focus on trigger phrases".
  Respect them; do not expand beyond what was asked.

## Review process (walk in this order)

1. **Read the candidate** SKILL.md. Note its frontmatter (`name`,
   `description`) and body structure (sections present, invocation form,
   inputs listed, downstream hand-offs named).
2. **Load target-plugin context**: read the target `plugin.json`, the
   plugin `README.md`, and each sibling `SKILL.md` under
   `<target>/skills/*/SKILL.md`. Identify the plugin's dominant
   conventions (section shape, invocation form, PT/EN trigger pattern).
3. **Verify every Ayunit reference LIVE** via the Ayunit MCP:
   - Every `mcp__ayunit__*` tool named in the candidate must exist.
     Confirm by inspecting the loaded MCP tool list; if a reference looks
     wrong, use `mcp__ayunit__list_procedures` /
     `mcp__ayunit__list_tables` / `mcp__ayunit__list_views` to
     suggest the correct name.
   - Every SQL Server object mentioned in the candidate
     (`Portfolio.X_Update`, `Global.v_Y`, `AssetDataDB.dbo.Z`) must exist.
     Verify via `mcp__ayunit__get_procedure_detail`,
     `mcp__ayunit__get_table_detail`, or `mcp__ayunit__get_view_detail`.
   - Every domain concept referenced (e.g. "checked date", "come cotas",
     "AssetRelated", "codigo_classe") must be traceable in the Ayunit
     docs. Verify via `mcp__ayunit__search_docs` before flagging.
4. **Fit assessment** — is this candidate a **new** skill or an
   **improvement to an existing sibling**? Compare its trigger prose,
   inputs, and body semantically against each sibling from step 2. Score
   overlap qualitatively (NONE / LOW / MEDIUM / HIGH). If HIGH on one
   sibling, recommend MERGE_INTO_SIBLING with a suggested consolidated
   description. If MEDIUM on multiple siblings, flag ambiguity risk (Claude
   will struggle to pick between them at runtime).
5. **House-convention check** (only rules actually observed in this repo;
   do not invent):
   - `name:` in frontmatter matches the containing folder name exactly.
   - `description:` is non-empty and reads like a "Use when …" trigger
     phrase, not a title.
   - If sibling skills carry PT + EN trigger phrases in their
     descriptions, the candidate should too.
   - If the candidate bundles `scripts/`, invocation lines in SKILL.md
     use `${CLAUDE_PLUGIN_ROOT}/skills/<name>/scripts/<file>` (the
     newer, location-independent form) — not bare relative paths.
   - MCP-router skills (no bundled Python) name the exact tool call and
     its required params.
   - Data-fetching skills name a downstream hand-off where one exists
     (e.g. cadastral fetch → `register-br-funds`).
   - Skills with close siblings include a "When NOT to trigger" section
     naming the sibling and its distinct scope.

## Output — the VERDICT report

Emit a single markdown block, no preamble, in this exact shape:

```
## VERDICT: <ACCEPT_NEW | MERGE_INTO_SIBLING | REJECT>

### Structural
- [✓/✗/!] one line per rule with the finding (line refs when applicable)

### Ayunit coherence (live-verified)
- ✓ mcp__ayunit__foo exists
- ✗ Portfolio.BarZ_Update NOT FOUND — did you mean Portfolio.Bar_Update? (candidate line N)
- ✓ concept "checked date" documented in transaction/faq
- (add a bullet per reference; be exhaustive within reason)

### Fit with target plugin (<plugin-name>)
Sibling skills read: skill-a, skill-b, skill-c
- vs skill-a — overlap: LOW  (distinct trigger, distinct params)
- vs skill-b — overlap: HIGH (both trigger on "puxa cadastro X"; both call
  the same MCP tool with the same shape) → merge candidate.

### Recommended actions
1. (specific, actionable — one bullet each)
2. …

### Suggested consolidated description (only if MERGE_INTO_SIBLING)
> "…the merged description text…"
```

## Guardrails

- **Do not modify the candidate file.** Emit findings only.
- **Do not fabricate references.** If a name isn't in the MCP surface or
  the docs, report it as unverified, don't invent an existence claim.
- **Respect the user's scope.** If they asked only about trigger overlap,
  don't dump the full structural section.
- **Cite line numbers** in findings whenever the candidate makes them
  useful (frontmatter typos, invocation-form issues, missing sections).
- **Under-report over over-report.** A short, sharp verdict beats a long
  ceremonial audit. Aim for ~40 lines of report; expand only when the
  candidate is complex.
