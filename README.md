# Sten Capital — Claude plugins marketplace (`sten-ayunit`)

Internal Claude marketplace for the **ayunit / Agnes backoffice**. One git repo,
one catalog (`.claude-plugin/marketplace.json`), and one plugin per Portfolio/Global
domain under `plugins/`. The team adds the marketplace **once**, then installs the
domain plugins they need and pulls updates with a single command.

## Layout

```
claude-plugins/                         (this repo → Azure DevOps)
├── .claude-plugin/
│   └── marketplace.json                catalog: lists every plugin + its source
├── plugins/
│   └── account-transaction/            domain plugin (one folder per core)
│       ├── .claude-plugin/plugin.json
│       ├── README.md
│       └── skills/
│           ├── btg-offshore/
│           ├── morgan-stanley/
│           ├── ubs-miami/
│           └── duplicate-trade-reconcile/
├── .claude/
│   └── settings.json.example           team auto-enable snippet (see below)
└── .gitignore
```

Each domain plugin is self-contained and independently versioned via its own
`plugin.json` `version`. Add future cores (`account-position`, `global-asset`, …)
as new `plugins/<name>/` folders and a new entry in `marketplace.json`.

## Repos & remotes (why there are two)

This repo lives on **two remotes**, kept in sync on every release:

| Remote | URL | Role |
|---|---|---|
| `origin` | `https://ayunit@dev.azure.com/ayunit/Agnes/_git/ayunit-plugins-marketplace` | **Source of truth.** Internal Azure DevOps repo under the `Agnes` project. |
| `github` | `https://github.com/StenCTO/ayunit-plugins-marketplace` | **Distribution mirror.** Public-account repo that teammates' Claude Desktop installs from. |

**Why a GitHub mirror exists:** Claude Desktop's `/plugin marketplace add` does
**not** accept Azure DevOps URLs as plugin sources. So we maintain the same
content on a GitHub repo as a distribution channel. Teammates install from the
GitHub URL; we still push to Azure as the internal source of truth.

**Push to both on every release** (the two remotes must not drift):

```bash
git push origin    # → Azure DevOps (source of truth)
git push github    # → GitHub mirror (what teammates install from)
```

If you cloned freshly and only see one remote, re-add the missing one:
`git remote add github https://github.com/StenCTO/ayunit-plugins-marketplace.git`.

## One-time: push this repo to Azure DevOps + GitHub mirror

Azure DevOps (internal project repo, source of truth) and a **GitHub mirror**
(`StenCTO/ayunit-plugins-marketplace`) — both kept in sync. The GitHub mirror
exists because Claude Desktop's plugin loader does **not** accept Azure DevOps
URLs, so teammates install from GitHub. Every release pushes to both:

```bash
cd "claude-plugins"
git init
git add .
git commit -m "Init sten-ayunit marketplace: account-transaction v0.7.0"
# source of truth — Azure DevOps
git remote add origin https://ayunit@dev.azure.com/ayunit/Agnes/_git/ayunit-plugins-marketplace
git push -u origin main
# distribution mirror — GitHub (what teammates install from)
git remote add github https://github.com/StenCTO/ayunit-plugins-marketplace.git
git push -u github main
```

Azure DevOps private repos authenticate with a **Personal Access Token** (PAT) via
your git credential manager. GitHub auth uses your normal credential manager / PAT.

On every release, `git push` goes to **origin (Azure)** and `git push github` to
the **GitHub mirror** — push to both so they don't drift.

## For each teammate: add the marketplace, install plugins

> Install from the **GitHub** URL — Claude Desktop doesn't accept Azure DevOps URLs.

```text
/plugin marketplace add https://github.com/StenCTO/ayunit-plugins-marketplace
/plugin install account-transaction@sten-ayunit
```

Pull later updates (after anyone pushes a new plugin version) with:

```text
/plugin marketplace update sten-ayunit
```

> Plugin skills are namespaced by plugin, e.g. `account-transaction:btg-offshore`,
> `account-transaction:duplicate-trade-reconcile`.

## Optional: auto-enable for the whole team (no manual install)

Commit a shared `.claude/settings.json` in your team's working repo (or distribute
it via policy) so the marketplace is pre-registered and plugins are enabled without
anyone touching the install UI. Copy `.claude/settings.json.example` to
`.claude/settings.json` where your team runs Claude:

```json
{
  "extraKnownMarketplaces": {
    "sten-ayunit": {
      "source": { "source": "url", "url": "https://github.com/StenCTO/ayunit-plugins-marketplace" }
    }
  },
  "enabledPlugins": { "account-transaction@sten-ayunit": true }
}
```

## Local MCP setup (for skill authors working in this repo)

When you're **authoring or debugging skills in this repo** (not running them as
an installed plugin), you can wire Claude Code in this folder directly to the
**ayunit MCP** so reads/writes hit the live DB during development. Copy the
example file to its real name and paste your ayunit API key:

```bash
cp .mcp.json.example .mcp.json
# then edit .mcp.json, replace REPLACE_WITH_YOUR_AYUNIT_API_KEY with your key
```

`.mcp.json` is git-ignored so the key never leaves your machine. Commit only
`.mcp.json.example`. Claude Code will offer to enable the `ayunit` MCP server
on the next session start.

## Releasing a change

1. Edit the skill(s) under `plugins/<domain>/skills/…`.
2. **Bump the plugin's `version`** in `plugins/<domain>/.claude-plugin/plugin.json`
   — users only receive an update when this field changes (if you omit `version`,
   every commit counts as a new version instead).
3. Commit, then push to **both** remotes: `git push && git push github`. Teammates
   run `/plugin marketplace update sten-ayunit` to pick up the new version from
   the GitHub mirror.

## Adding a new domain plugin

```bash
mkdir -p plugins/account-position/.claude-plugin plugins/account-position/skills
# write plugins/account-position/.claude-plugin/plugin.json  (name, version, description)
# add skills under plugins/account-position/skills/<skill>/SKILL.md
```

Then add it to `.claude-plugin/marketplace.json`:

```json
{
  "name": "account-position",
  "source": "./plugins/account-position",
  "description": "…"
}
```

## Current contents

| Plugin | Version | Skills |
|---|---|---|
| `account-transaction` | 0.9.0 | `btg-offshore`, `morgan-stanley`, `ubs-miami`, `duplicate-trade-reconcile`, `compromissada-fix`, `assetrelated-fix` |
| `position` | 0.1.0 | `inception-position` |
| `routines` | 0.1.0 | `daily-btg-offshore-routine` |
