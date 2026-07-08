---
name: anbima-funds-data
description: "Use when the user wants ANBIMA cadastral data for a Brazilian investment fund by ANBIMA code (PT: 'puxa dados ANBIMA do fundo X', 'busca cadastro ANBIMA do fundo Y'; EN: 'get ANBIMA fund data for code Z'). This endpoint is fund-specific (not for CRIs, CRAs, debêntures, or other securities — those have other sources). Routes via source=agnes (default — hits the internal Agnes API server, which handles the OAuth token dance) or source=anbima (direct call to api.anbima.com.br using ANBIMA_CLIENT_ID/SECRET env vars; requires the ANBIMA_CADASTRAL_URL constant to be filled in). Returns the raw ANBIMA JSON response so downstream skills (e.g. asset plugin's register-br-funds) can consume it as-is."
---

# Fetch ANBIMA fund cadastral data by code

Fetches ANBIMA's cadastral registry for a single **Brazilian investment fund**
identified by its ANBIMA code (nome, CNPJ, classification, gestor,
administrador, etc.). Fund-scoped only — do not use this skill for CRIs,
CRAs, debêntures, or equity.

## Inputs

- `--codigo` (required) — the fund's ANBIMA `codigo_classe` (e.g. `C0000751243`) or, for multiclasse funds, its `codigo_subclasse`. **Not** the CNPJ, not the legacy `258.363`-style code, and not the `codigo_fundo` (`F…`).
- `--source` (optional, default `agnes`) — `agnes` (via Agnes API proxy) or
  `anbima` (direct against `api.anbima.com.br`).

## How to invoke

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/anbima-funds-data/scripts/fetch_funds_data.py" \
  --codigo C0000751243 --source agnes
```

Output: JSON on stdout, exit 0. On HTTP error: error text on stderr, exit 1.

## Source routing

- **`source=agnes`** (default). Always prefer unless the user explicitly asks
  otherwise. Agnes is reachable from the corporate network and handles ANBIMA
  auth server-side. Requires `AGNES_API_KEY` in `plugins/anbima-data/.env`.
- **`source=anbima`**. Only if the user says so or Agnes is down. Requires
  `ANBIMA_CLIENT_ID` / `ANBIMA_CLIENT_SECRET` in `plugins/anbima-data/.env` (or
  in the process env) **and** the `ANBIMA_CADASTRAL_URL` constant filled in at
  [shared/_anbima_client.py](../../shared/_anbima_client.py). Fails cleanly
  with a pointer if either is missing.

## Error handling

Every non-200 surfaces the response body verbatim on stderr and the script
exits 1. No silent fallback — the user (or you) sees which source failed and
why.

## Downstream

If the user then wants to **register** the fetched fund into `Global.Asset`,
hand off to the `asset` plugin's `register-br-funds` skill, which takes the
CNPJ / ANBIMA code and drives the peer-lookup + INSERT.
