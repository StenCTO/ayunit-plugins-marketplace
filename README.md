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

## One-time: push this repo to Azure DevOps

You said you keep one repo per project. Create a new repo for the marketplace
(e.g. `claude-plugins`) in whichever project you want to own the tooling:

```bash
cd "claude-plugins"
git init
git add .
git commit -m "Init sten-ayunit marketplace: account-transaction v0.7.0"
git remote add origin https://ayunit@dev.azure.com/ayunit/Agnes/_git/ayunit-plugins-marketplace
git push -u origin main
```

Azure DevOps private repos authenticate with a **Personal Access Token** (PAT) via
your git credential manager — the same credentials you use to clone any repo. If
`git push` prompts, use your PAT as the password.

## For each teammate: add the marketplace, install plugins

```text
/plugin marketplace add https://dev.azure.com/ayunit/Agnes/_git/ayunit-plugins-marketplace
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
      "source": { "source": "url", "url": "https://dev.azure.com/ayunit/Agnes/_git/ayunit-plugins-marketplace" }
    }
  },
  "enabledPlugins": { "account-transaction@sten-ayunit": true }
}
```

## Releasing a change

1. Edit the skill(s) under `plugins/<domain>/skills/…`.
2. **Bump the plugin's `version`** in `plugins/<domain>/.claude-plugin/plugin.json`
   — users only receive an update when this field changes (if you omit `version`,
   every commit counts as a new version instead).
3. Commit and push. Teammates run `/plugin marketplace update sten-ayunit`.

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
| `account-transaction` | 0.7.0 | `btg-offshore`, `morgan-stanley`, `ubs-miami`, `duplicate-trade-reconcile` |
