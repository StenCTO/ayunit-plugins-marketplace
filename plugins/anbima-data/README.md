# anbima-data

ANBIMA-related skills for Brazilian investment funds. Every skill is a
thin routing layer over the **Ayunit MCP** — the Ayunit backend holds the
ANBIMA client-id/secret and handles the OAuth token dance server-side, so
this plugin ships no code, no credentials, and no `.env`.

Cross-surface by design: works in Claude Code, Claude Desktop, and Claude
Cowork wherever the Ayunit MCP is loaded. Live Artifact skills are
interactive only in Cowork.

## Skills

| Skill | What it does | Cross-surface |
|---|---|---|
| `anbima-funds-data` | Fetch cadastral registry for a fund via `mcp__ayunit__get_anbima_cadastral_data` | ✅ everywhere |
| `anbima-funds-historical-data` | Fetch NAV/PL/cota time series via `mcp__ayunit__get_anbima_historical_data` | ✅ everywhere |
| `anbima-funds-explorer` | Emit an interactive Live Artifact dashboard to browse funds and inspect historical series | 🟡 renders anywhere; live/interactive **only in Cowork** |

## Requirements

The Ayunit MCP must be loaded in the session. That's it — no env vars, no
credentials, no local dependencies.

## Identifier semantics (data skills)

Both `anbima-funds-data` and `anbima-funds-historical-data` expect the fund's
ANBIMA `codigo_classe` (`C…`) or, for multiclasse funds, the
`codigo_subclasse` (`S…`). **Not** the CNPJ, **not** the `codigo_fundo`
(`F…`), and **not** legacy `258.363`-style codes. If the user gives a CNPJ,
resolve it to an ANBIMA code first via `mcp__ayunit__identify_asset` or a
lookup on `Global.v_Asset.AnbimaCode`.

## Live Artifacts

Live Artifacts live at `live-artifacts/*.html` in the plugin root. Each is a
self-contained HTML+JS shell that calls the Ayunit MCP at runtime via
Cowork's `window.cowork.callMcpTool` bridge, so the same file renders as an
interactive dashboard for every user with the plugin + MCP loaded.

Files currently shipped:
- `live-artifacts/fundos-explorer.html` — the funds browser (paired with the
  `anbima-funds-explorer` skill).

Adding a new one: drop the HTML in `live-artifacts/`, add a matching
`<name>` skill folder whose SKILL.md tells Claude to read the file and emit
it as an Artifact.
