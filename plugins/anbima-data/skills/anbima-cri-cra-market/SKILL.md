---
name: anbima-cri-cra-market
description: "Use when the user wants the ANBIMA CRI/CRA secondary-market snapshot (PT: 'mercado secundário CRI/CRA ANBIMA', 'preços de CRI CRA no secundário'; EN: 'ANBIMA CRI CRA secondary market'). Optional data_referencia (YYYY-MM-DD) picks a specific date; without it, the server returns its default (usually the most recent business day). Routes via source=agnes (default — hits Agnes API server GET auto-token variant) or source=anbima (direct against api.anbima.com.br; requires the ANBIMA_CRI_CRA_URL constant to be filled in). Returns the raw ANBIMA JSON response."
---

# Fetch ANBIMA CRI/CRA secondary-market snapshot

Pulls the ANBIMA CRI/CRA secondary-market feed for a given reference date.
Used for pricing/marking CRI/CRA positions and for spot-checking the book
against the published secondary market.

## Inputs

- `--data-referencia` (optional, `YYYY-MM-DD`) — reference date. Omit for server default.
- `--source` (optional, default `agnes`) — `agnes` or `anbima`.

## How to invoke

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/anbima-cri-cra-market/scripts/fetch_cri_cra.py" \
  --data-referencia 2026-07-01 --source agnes
```

Output: JSON on stdout, exit 0. On HTTP error: error text on stderr, exit 1.

## Source routing

- **`source=agnes`** (default). Preferred — Agnes handles ANBIMA OAuth server-side.
- **`source=anbima`**. Requires `ANBIMA_CLIENT_ID` / `ANBIMA_CLIENT_SECRET` in `plugins/anbima-data/.env` and the `ANBIMA_CRI_CRA_URL` constant filled in at [shared/_anbima_client.py](../../shared/_anbima_client.py). Fails cleanly with a pointer otherwise.

## Error handling

Non-200 response bodies pass through to stderr verbatim, script exits 1.
