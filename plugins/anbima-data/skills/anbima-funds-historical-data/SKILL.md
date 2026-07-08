---
name: anbima-funds-historical-data
description: "Use when the user wants the ANBIMA historical series (PL, cota, rentabilidade, etc.) for a Brazilian investment fund by ANBIMA code (PT: 'puxa histórico ANBIMA do fundo X', 'série histórica ANBIMA do fundo Y'; EN: 'get ANBIMA fund historical for code Z'). Supports optional page size and start date. Fund-specific — do not use for CRI/CRA/debêntures. Routes via source=agnes (default — hits Agnes API server GET auto-token variant) or source=anbima (direct against api.anbima.com.br/feed/fundos/v2/fundos/{codigo}/serie-historica, using ANBIMA_CLIENT_ID/SECRET env vars). Returns the raw ANBIMA JSON response."
---

# Fetch ANBIMA fund historical series by code

Fetches the ANBIMA historical time series for a single **Brazilian investment
fund** by ANBIMA code (PL, cota, rentabilidade, captação, resgate, etc.).
Useful for backfilling `AssetData` price history or spot-checking a fund's
published NAV/cota against our book. Fund-scoped only.

## Inputs

- `--codigo` (required) — the fund's ANBIMA `codigo_classe` (e.g. `C0000751243`) or, for multiclasse funds, its `codigo_subclasse`. **Not** the CNPJ and not the `codigo_fundo` (`F…`).
- `--size` (optional int) — number of records to return (defaults to ANBIMA server default).
- `--data-inicio` (optional str, `YYYY-MM-DD`) — earliest date to include.
- `--source` (optional, default `agnes`) — `agnes` or `anbima`.

## How to invoke

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/anbima-funds-historical-data/scripts/fetch_funds_historical_data.py" \
  --codigo C0000751243 --size 30 --data-inicio 2026-01-01 --source agnes
```

Output: JSON on stdout, exit 0. On HTTP error: error text on stderr, exit 1.

## Source routing

- **`source=agnes`** (default). Agnes handles ANBIMA OAuth server-side; requires only `AGNES_API_KEY` in `plugins/anbima-data/.env`. Query params use underscore (`data_inicio`).
- **`source=anbima`**. Direct call to `https://api.anbima.com.br/feed/fundos/v2/fundos/{codigo}/serie-historica`. Requires `ANBIMA_CLIENT_ID` / `ANBIMA_CLIENT_SECRET` in `.env`; the client sends both `client_id` and `access_token` headers per ANBIMA's Sensedia gateway. Query params use hyphen (`data-inicio`).

## Error handling

Non-200 response bodies pass through to stderr verbatim, script exits 1. No silent fallback between sources.
