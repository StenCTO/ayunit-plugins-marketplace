# marketplace-tools

Meta-plugin holding review skills for the sten-ayunit marketplace. Used
interactively in Claude Code chat to audit candidate contributions
(SKILL.md files, HTML live-artifacts) *before* they land in a domain
plugin — whether the candidate came from a colleague or from your own
authoring session.

Both review skills are **interactive** and **scoped by the user prompt**.
They do not run in CI, do not modify the candidate file, and do not
require any external service beyond the Ayunit MCP (which the skills use
live to verify referenced tools, procedures, tables, and docs).

## Skills

| Skill | Reviews | Emits |
|---|---|---|
| `skill-review` | Candidate `SKILL.md` at any path | Markdown VERDICT report + line-referenced findings |
| `artifact-review` | Candidate HTML at any path (typically `live-artifacts/*.html`) | Markdown VERDICT report + line-referenced findings |

## Requirements

- The **Ayunit MCP** must be loaded — the reviewers use `list_procedures`,
  `list_tables`, `list_views`, `search_docs`, and per-tool schemas to
  verify that every reference in the candidate resolves to something real.
- No credentials, no bundled Python, no `.env`.

## Placement convention (candidates)

Drop the candidate **in-place** at its intended path inside the target
plugin (e.g. `plugins/anbima-data/skills/new-fund-flow/SKILL.md`). The
review skill picks up its target plugin from the path.

## Verdict shape

Every review ends with one of:

- **`ACCEPT_NEW`** — candidate is a coherent addition; specific, minor
  fixes may be listed.
- **`MERGE_INTO_SIBLING`** — candidate substantially overlaps an existing
  skill in the target plugin; the review names the sibling and suggests
  the consolidated form.
- **`REJECT`** — candidate is broken, out of scope, or duplicates an
  existing capability with no additive value. Findings explain why.
