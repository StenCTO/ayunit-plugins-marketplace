---
name: anbima-funds-data
description: "Use when the user wants ANBIMA cadastral data for a Brazilian investment fund by ANBIMA code (PT: 'puxa dados ANBIMA do fundo X', 'busca cadastro ANBIMA do fundo Y', 'dados cadastrais ANBIMA do fundo'; EN: 'get ANBIMA fund data for code Z', 'fetch ANBIMA cadastral for fund'). Fund-scoped only — do not use for CRIs, CRAs, debêntures, or equity. The skill calls the Ayunit MCP tool `get_anbima_cadastral_data`, which the Ayunit backend proxies to ANBIMA server-side (auth handled there). Cross-surface: works in Claude Code, Claude Desktop, and Claude Cowork."
---

# Fetch ANBIMA fund cadastral data via the Ayunit MCP

Fetches ANBIMA's cadastral registry for a single **Brazilian investment
fund**: nome/razão social, CNPJ, taxonomy (categoria/subcategoria/tipo_fundo),
administrador/gestor, data de constituição, moeda, and other registry
attributes ANBIMA publishes and the Ayunit backend mirrors.

## Inputs

- **anbima_code** (required) — the fund's ANBIMA `codigo_classe` (e.g.
  `C0000000191`) or, for multiclasse funds, the `codigo_subclasse` (e.g.
  `S0000730300`). **Not** the CNPJ, **not** the `codigo_fundo` (`F…`), and
  **not** a legacy `258.363`-style code. If the user gives a CNPJ or any
  other identifier, resolve it first via `mcp__ayunit__identify_asset` or a
  `Global.v_Asset.AnbimaCode` lookup before calling this skill.

## How to invoke

Call the MCP tool directly:

```
mcp__ayunit__get_anbima_cadastral_data(anbima_code="C0000000191")
```

The tool returns the raw ANBIMA JSON. No Python, no credentials, no `.env`
— the Ayunit backend holds the ANBIMA client-id/secret and handles the
OAuth token dance server-side.

## Downstream

If the user then wants to **register** the fetched fund into `Global.Asset`,
hand off to the `asset` plugin's `register-br-funds` skill, which takes the
CNPJ / ANBIMA code and drives the peer-lookup + INSERT.

## Errors surface upstream

- ANBIMA `403 Acesso negado` → the code isn't in ANBIMA's subscription for
  this environment, or you passed a `codigo_fundo` (`F…`) instead of a
  `codigo_classe`/`codigo_subclasse`. Verify the identifier and retry.
- ANBIMA `404` → code doesn't exist. Double-check with the user.
- Any MCP-level error → surface verbatim; don't retry silently.
