# CLAUDE.md — `sten-ayunit` marketplace working notes

Operating rules for editing this plugin marketplace repo. Human-facing docs
(what the marketplace is, how teammates install it) live in [README.md](README.md).
Read this before editing any plugin, skill, or `marketplace.json`.

---

## 0. Golden rules

- **Bump the plugin's `version` on every user-visible change.** Teammates only
  receive updates when `plugins/<domain>/.claude-plugin/plugin.json` `version`
  changes. Skipping the bump silently strands the fix.
- **Land every release via a PR merged to `main`, never a direct push.** The
  Cowork Desktop marketplace sync auto-fires only on PR merges to the default
  branch — direct pushes to `main` are silently invisible to teammates' Cowork
  Desktops and strand the version bump on GitHub even though the commit
  landed. See §3.5 for the concrete release workflow.
- **Push to BOTH remotes on every release.** `origin` = Azure DevOps (source of
  truth), `github` = distribution mirror teammates install from. "Push" means
  the branch reaches both remotes AND the PR merge on `github` is mirrored to
  `origin/main` (see §3.5 step 6). Never leave one remote behind.
- **Never commit `.mcp.json`.** It contains the ayunit API key. `.mcp.json` is
  git-ignored; only `.mcp.json.example` is committed. If you see `.mcp.json` in
  `git status`, stop and check `.gitignore` before doing anything else.
- **Preserve skill frontmatter.** `SKILL.md` files start with a YAML block
  (`name`, `description`, sometimes `allowed-tools`). The `description` is what
  Claude uses to decide when to invoke the skill — edit it deliberately, and
  never delete the frontmatter.
- **Docs must match reality before every commit.** If you added/removed/renamed
  a skill, bumped a version, or changed a plugin's purpose, every `*.md` that
  references it must be updated in the same commit — the repo `README.md`
  ("Current contents" table + layout diagram), the plugin's own `README.md`,
  and any cross-referencing skill body. See §4 step 6 for the concrete
  grep-verification step. Silent doc drift is treated as a bug, not a
  follow-up.

## 1. Repo layout at a glance

```
claude-plugins/
├── .claude-plugin/marketplace.json   catalog — every plugin must be listed here
├── plugins/<domain>/
│   ├── .claude-plugin/plugin.json    name, version, description (bump version!)
│   ├── README.md                     human-facing plugin overview
│   └── skills/<skill>/
│       ├── SKILL.md                  machine-facing skill (YAML frontmatter + body)
│       └── references/               optional deep-dive docs the skill links to
└── .mcp.json.example                 template — real .mcp.json is git-ignored
```

Current domains: `account-transaction`, `position`, `asset`, `routines`,
`anbima-data`, `marketplace-tools`. Add new domains as `plugins/<name>/` +
an entry in `.claude-plugin/marketplace.json`.

## 2. Versioning discipline (SemVer within a plugin)

Each plugin versions independently in its own `plugin.json`.

| Change | Bump |
|---|---|
| Typo, README wording, comment cleanup | none (or patch if unsure) |
| Skill body edit that changes behavior | patch (`0.11.1 → 0.11.2`) |
| New skill added to an existing plugin | minor (`0.11.x → 0.12.0`) |
| Breaking rename / removed skill / changed skill contract | major (`0.x → 1.0`, or clearly note in commit) |

If you omit `version` entirely, every commit counts as a new version — don't
do that.

## 3. Commit message style

Follow the recent history — one line per plugin touched, `<plugin> v<ver>:`
prefix, then a terse summary. Multi-plugin commits chain with `+`:

```
asset v0.5.0 + routines v0.2.8 + account-transaction v0.11.1: assetcustody-fill leaf + user-end_date discipline + duplicate two-phase autonomy
```

Rules:
- Include every plugin whose `version` bumped.
- Summary in the imperative, no trailing period.
- No emojis. No "Generated with Claude Code" footers.

## 3.5 Release workflow — PR-to-`main`, mirror to Azure

**Why this exists.** Cowork Desktop's marketplace CDN auto-syncs only on PR
merges to the default branch. A direct `git push origin main` (or `git push
github main`) lands the commit but never notifies the CDN — teammates see the
old "Last updated" date indefinitely, even after restarting Desktop. This
happened between 2026-07-20 and 2026-07-31: five commits stranded, four
plugin version bumps invisible.

The workflow below adds ~30 seconds per release and mechanically prevents
that failure.

```bash
# 1. Branch from main for the change
git checkout main && git pull github main
git checkout -b <short-descriptor>              # e.g. bump-workday-audit-dedup

# 2. Edit, bump every changed plugin's version, doc-coherence grep (§4 step 6)
#    Commit with the §3 message style
git add <files>
git commit -m "<plugin> v<X.Y.Z>: <summary>"

# 3. Push the branch to BOTH remotes
git push github <short-descriptor>              # required — triggers PR flow
git push origin <short-descriptor>              # keeps Azure in sync

# 4. Open a PR on GitHub → merge it (squash + delete branch)
#    URL: https://github.com/StenCTO/ayunit-plugins-marketplace/pull/new/<short-descriptor>
#    Merging to `main` on GitHub is what fires the marketplace CDN sync.

# 5. Locally fast-forward main to the merged commit
git checkout main
git fetch github && git merge --ff-only github/main
git branch -d <short-descriptor>

# 6. Mirror the merged commit to Azure so both remotes stay identical
git push origin main
```

**Belt-and-suspenders.** Enable **branch protection on `main`** in GitHub
Settings → Branches → Add rule → "Require a pull request before merging".
Once on, GitHub rejects any direct `git push github main`, mechanically
enforcing this workflow. Recommended even for a solo maintainer — it removes
the failure mode entirely.

**When teammates get the update.** After step 4 merges, the marketplace CDN
picks up the new manifest on its next sync (typically minutes). Teammates
receive the new plugin versions on their **next Cowork Desktop restart**,
provided their marketplace has "Sync automatically" enabled (a one-time
per-teammate setup: Settings → Marketplaces → sten-ayunit → toggle on).

**Exceptions to PR flow.** None for plugin changes. Docs-only edits to root
`README.md` or this `CLAUDE.md` may go via PR too for consistency, but a
direct push doesn't strand anything user-facing — no version bump means no
plugin update to propagate.

## 4. Editing a skill — checklist

1. Edit `plugins/<domain>/skills/<skill>/SKILL.md` (or a `references/*.md` it links to).
2. **Consult the ayunit MCP docs** for every domain concept the skill touches
   (see §5). Hard rule for new skills and behaviour-changing edits; optional
   for cosmetic edits.
3. Bump `plugins/<domain>/.claude-plugin/plugin.json` `version`.
4. If the skill's *purpose* or *trigger phrase* changed, update its `description`
   in the YAML frontmatter — that's what routes future invocations to it.
5. If a skill was **added** or **removed**, update the plugin's `README.md`
   skill list AND the "Current contents" table in the repo root [README.md](README.md).
6. **Doc-coherence grep** — before committing, verify no doc still refers to
   the old reality:
   ```bash
   # renamed/removed a skill? make sure nothing still names it
   grep -rn "<old-skill-name>" --include="*.md" .
   # bumped a version? make sure the README table matches
   grep -n "<plugin-name>" README.md
   ```
   Fix anything stale in the SAME commit.
7. Commit + release via PR-to-`main` **on both remotes** (see §0 + §3.5).

## 5. Skills & the ayunit MCP docs (source of truth for domain concepts)

The ayunit MCP exposes a doc catalogue via `list_docs`, `search_docs`, and
`read_doc`. These docs are the **canonical description** of Portfolio / Global
domain concepts (transaction lifecycle, procedure shapes, CheckedDate,
PortfolioCreator pipeline, etc.). A skill that contradicts a doc will produce
broken writes — the doc is right, the skill is wrong.

Two-tier discipline (mirrors §2 versioning):

**Hard rule — new skill OR behaviour-changing edit:**
1. Before writing, call `search_docs` (or `list_docs` if you don't know the slug)
   for every domain concept the skill touches.
2. Read the top matches with `read_doc`.
3. Cite the doc slug(s) in the skill body where you rely on them
   (e.g. "per `transaction/procedure`, `U` overwrites every column").
4. If the skill and the doc disagree, STOP and surface the conflict to the user
   — don't silently pick one.

**Soft — cosmetic edits (typo, wording, README polish):**
Docs consultation optional. Version bump per §2 still applies.

**Validation / review:** when auditing an existing skill
(e.g. via `marketplace-tools:skill-review`), the docs are also the reference —
a skill whose claims can't be found in `search_docs` for the concept it names
is a finding worth flagging.

## 6. Adding a new plugin

1. `plugins/<name>/.claude-plugin/plugin.json` — `name`, `version` (start at
   `0.1.0`), `description`.
2. `plugins/<name>/README.md` — human-facing overview.
3. `plugins/<name>/skills/<skill>/SKILL.md` — at least one skill (follow §5
   docs discipline).
4. Add an entry to `.claude-plugin/marketplace.json`:
   ```json
   { "name": "<name>", "source": "./plugins/<name>", "description": "…" }
   ```
5. Update the "Current contents" table in [README.md](README.md).
6. Run the doc-coherence grep from §4 step 6 before committing.

## 7. Do NOT

- Commit `.mcp.json`, `.env`, or any file with an API key.
- Force-push to `main` on either remote.
- **Direct-push a plugin change to `main` on `github`** — the marketplace CDN
  won't sync (see §3.5). Always land plugin changes via a PR merge on GitHub.
- Push to only one remote — they must not drift.
- Edit a skill without bumping the plugin version (silent stranded fix).
- Remove YAML frontmatter from a `SKILL.md`.
- Rename a plugin folder without updating `marketplace.json` `source`.
- Commit a skill/plugin change while leaving a `README.md` still describing the
  old reality (see §0 doc-coherence rule).

## 8. When in doubt

- Skill authoring conventions → the `marketplace-tools:skill-review` skill.
- Domain concepts (transaction lifecycle, procedures, CheckedDate, pipeline)
  → ayunit MCP docs via `search_docs` / `read_doc` (see §5).
- Anything DB-related (queries against `AgnesOrg00DB`, procedure shapes,
  CheckedDate) → the parent `CLAUDE.md` one level up in `AccountTransaction/`
  (present on the author's machine; not in this repo).
