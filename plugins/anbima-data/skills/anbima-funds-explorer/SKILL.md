---
name: anbima-funds-explorer
description: "Use when the user wants an interactive live dashboard / explorer to browse Brazilian investment funds (PT: 'abre o explorer dos fundos', 'painel interativo de fundos', 'dashboard de fundos ANBIMA', 'quero navegar pelos fundos'; EN: 'open the funds explorer', 'live funds dashboard', 'interactive funds browser'). Emits a self-contained HTML Live Artifact that calls the Ayunit MCP at runtime via `window.cowork.callMcpTool` — the artifact lists funds from AssetDataDB, supports sort/filter/column-pick, and has a historical panel powered by mcp__ayunit__get_anbima_historical_data. Live-interactive only in Claude Cowork (the JS bridge is Cowork-specific); in Claude Code / Claude Desktop the artifact still renders but its MCP-backed buttons will fail. Requires the Ayunit MCP to be loaded in the session."
---

# Open the Brazilian funds Live Artifact explorer

Emits a self-contained HTML dashboard as a Live Artifact. The artifact is a
single HTML file that ships in this plugin at
`${CLAUDE_PLUGIN_ROOT}/live-artifacts/fundos-explorer.html`; it uses
`window.cowork.callMcpTool` to fetch data from the Ayunit MCP at runtime:

- Base fund list via `mcp__ayunit__execute_select_query` against
  `AssetDataDB`.
- Per-fund historical series via
  `mcp__ayunit__get_anbima_historical_data`.

Because the artifact calls MCP tools by name (not by URL or credentials),
every worker with the Ayunit MCP loaded gets the same interactive
experience against their own MCP session.

## How to invoke

1. Read the HTML file verbatim:
   ```
   ${CLAUDE_PLUGIN_ROOT}/live-artifacts/fundos-explorer.html
   ```
2. Emit its **entire content** as an Artifact of type `text/html` (title:
   *Fundos Investimento Brasil*). Do not modify or paraphrase the HTML —
   the JS bridge relies on the exact structure.
3. In your reply, mention the artifact is Cowork-optimised: the MCP-backed
   panels work only in Cowork; other surfaces render the layout but the
   dashboard's data calls will not resolve.

## When NOT to trigger

- User asks for cadastral data of a specific fund by code → use
  `anbima-funds-data` instead (targeted, no dashboard).
- User asks for the historical series of a single fund → use
  `anbima-funds-historical-data` instead.
- User is in a session without the Ayunit MCP loaded → emit the artifact
  anyway so they see the layout, but flag upfront that the data panels
  won't populate until the MCP is added.
