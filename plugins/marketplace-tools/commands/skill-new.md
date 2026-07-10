---
description: Draft a new Ayunit-aware SKILL.md — Anthropic skill-creator flow reworked to surface the Ayunit MCP doc catalogue up-front and live-verify every reference before the file is written.
argument-hint: [target plugin path or short intent, e.g. plugins/account-transaction "reconcile lending fees"]
---

# /skill-new — author a new SKILL.md, Ayunit-aware

You are running the **skill-creator** flow, reformulated for this
repository. The goal is a single new `SKILL.md` file placed at
`plugins/<plugin>/skills/<skill-name>/SKILL.md`, whose frontmatter and
body follow the house conventions used across the repo and whose Ayunit
references are grounded in the **live** MCP catalogue — not guessed.

User arguments: `$ARGUMENTS`

## Non-negotiables

- **Do not write the file until the user confirms** the drafted
  frontmatter + section outline.
- **Every `mcp__ayunit__X` tool, `Portfolio.*` / `Global.*` /
  `AssetDataDB.*` object, and `ayunit://docs/…` URI cited in the draft
  must be verified live** before the file is written. Unverified
  references are removed or rewritten, never left dangling.
- **Never invent tools, procedures, tables, views, or doc URIs** from
  training data. If it isn't in the live MCP surface *now*, it doesn't
  go in the SKILL.md.
- Placement is `plugins/<plugin>/skills/<skill-folder>/SKILL.md`. The
  frontmatter `name:` **must equal** the containing folder name.

## The 5-phase flow

### Phase 1 — Intent

Read `$ARGUMENTS`. Extract (or ask the user for, one question at a
time, only what's missing):

1. **Target plugin** — path under `plugins/`. If the user named a plugin
   that doesn't exist, list existing plugins and ask.
2. **Skill purpose** — one sentence: *what defect / task does this
   skill solve, and when should Claude trigger it?*
3. **Trigger phrases** — the natural PT/EN wording the user (or
   colleagues) will actually say when they want this skill to fire.
   Sibling plugins on this repo use bilingual triggers; match that.
4. **Scope of writes** — read-only, single-row `@CMD='U'`, batch,
   destructive (`@CMD='D'`)? Determines the guardrails section shape.

Do **not** proceed until all four are pinned down.

### Phase 2 — Ground the Ayunit surface (live)

Call the MCP to build the reference set the draft is allowed to cite:

1. `mcp__ayunit__list_docs` → present a compact index (URI +
   one-line description) filtered to the domain(s) relevant to the
   stated purpose (e.g. transaction/*, position/*, checkeddate/*).
   Ask the user to confirm which URIs the skill should point at.
2. For each SQL Server object the purpose implies
   (`Portfolio.X_Update`, `Global.v_Y`, etc.), verify existence with
   `mcp__ayunit__get_procedure_detail` / `get_table_detail` /
   `get_view_detail`. If a guess fails, use `list_procedures` /
   `list_tables` / `list_views` to find the real name and confirm with
   the user.
3. For every domain concept the purpose hinges on ("checked date",
   "come cotas", "AssetRelated", "codigo_classe", "AvgPrice"), run
   `mcp__ayunit__search_docs` and record which doc URI(s) cover it.
   If none does, flag it — the skill must not lean on undocumented
   concepts as if they were canonical.
4. Load target-plugin context: read the plugin's `plugin.json`,
   `README.md`, and each sibling `SKILL.md` under
   `plugins/<plugin>/skills/*/SKILL.md`. Note the plugin's dominant
   section shape, invocation form, PT/EN trigger pattern, and any
   downstream hand-offs siblings name.

Emit a short **Ayunit reference set** the draft will be allowed to
cite. Anything outside this set requires re-verification before it
lands in the file.

### Phase 3 — Draft frontmatter + outline

Produce, inline in chat (do **not** write the file yet):

```
---
name: <skill-folder-name>          # must equal the folder you'll place it in
description: "Use when …"          # one dense sentence; PT + EN triggers if siblings use them; name the sibling to NOT trigger for when close ones exist
---

# <title>

## Inputs
## Reference resources (read on demand)     # table pointing at the verified ayunit:// URIs from Phase 2
## Tools you call directly                   # only MCP tools verified in Phase 2
## The <n>-step cycle                        # scope → verify → resolve → gate → write → verify
## Critical rules
## When unsure                               # named edge cases with dispositions
```

Fit the outline to the write scope from Phase 1:
- **Read-only** skills drop the CheckedDate gate and the SELECT-first-merge
  guardrail.
- **`@CMD='U'` skills** must call out: SELECT-first-merge, drop
  `AccountCurrency` / `AccountFx`, absolute values, `AgentCheck` on every
  write, per-row lock gate on `Date` **and** `SettlementDate`.
- **Batch writes** additionally require the `dry_run=true` → review →
  `dry_run=false` protocol.
- **Destructive (`@CMD='D'`)** additionally requires
  `allow_destructive=true` and an explicit user confirmation step.

Present the outline; wait for user approval or edits.

### Phase 4 — Coherence pass before writing

Once the user approves the outline, do a final live re-check:

- Every `mcp__ayunit__*` in the draft body → still in the loaded MCP
  tool list.
- Every `Portfolio.*` / `Global.*` / `AssetDataDB.*` object → still
  resolvable via the corresponding `get_*_detail`.
- Every `ayunit://docs/…` URI → still in `list_docs`.
- `name:` in frontmatter == the folder segment you're about to write to.
- If the plugin uses `${CLAUDE_PLUGIN_ROOT}/skills/<name>/scripts/…`
  invocation form, the draft uses it too (not bare relative paths).
- If close sibling(s) exist, the draft has a **"When NOT to trigger"**
  section naming the sibling and its distinct scope.

Any check that fails → fix or drop before proceeding.

### Phase 5 — Write the file

Only after Phase 4 passes clean:

1. Confirm the target path with the user one last time
   (`plugins/<plugin>/skills/<skill-folder>/SKILL.md`).
2. Write the file with the Write tool.
3. Emit a short summary: path, cited MCP tools, cited doc URIs, cited
   SQL objects, and a suggested next step —
   **run `/skill-review <path>` (or invoke the `skill-review` skill)**
   to audit the freshly written draft against sibling conventions before
   committing.

## Guardrails

- **One question at a time** during Phase 1 — don't dump a survey.
- **Never write the file across a phase gate the user hasn't approved.**
- **Do not modify sibling skills** while drafting a new one. If Phase 2
  reveals the candidate substantially overlaps a sibling, stop and tell
  the user — they may want to *edit the sibling* instead of adding a new
  skill (that's the `skill-review` verdict `MERGE_INTO_SIBLING`
  workflow, not this command's job).
- **Reply in the user's language** (PT/EN). Match the phrasing style of
  sibling skills in the target plugin.
- **Under-write over over-write.** A SKILL.md that fits on one screen
  and points cleanly at the right doc URIs beats a long ceremonial one.
