# anbima-data

ANBIMA data-feed skills for Brazilian investment funds. Every skill is a
thin routing layer over the **Ayunit MCP** — the Ayunit backend holds the
ANBIMA client-id/secret and handles the OAuth token dance server-side, so
this plugin ships no code, no credentials, and no `.env`.

Cross-surface by design: works in Claude Code, Claude Desktop, and Claude
Cowork wherever the Ayunit MCP is loaded.

## Skills

| Skill | MCP tool | Purpose |
|---|---|---|
| `anbima-funds-data` | `mcp__ayunit__get_anbima_cadastral_data` | Cadastral registry for a Brazilian investment fund by ANBIMA code (fund-only) |
| `anbima-funds-historical-data` | `mcp__ayunit__get_anbima_historical_data` | Fund historical series (`valor_cota`, `valor_patrimonio_liquido`, subscriptions/redemptions, `numero_cotistas`) with optional `data_inicio` + `size` |

## Requirements

The Ayunit MCP must be loaded in the session. That's it — no env vars, no
credentials, no local dependencies.

## Identifier semantics

Both skills expect the fund's ANBIMA `codigo_classe` (`C…`) or, for
multiclasse funds, the `codigo_subclasse` (`S…`). **Not** the CNPJ, **not**
the `codigo_fundo` (`F…`), and **not** legacy `258.363`-style codes. If the
user gives a CNPJ, resolve it to an ANBIMA code first via
`mcp__ayunit__identify_asset` or a lookup on `Global.v_Asset.AnbimaCode`.
