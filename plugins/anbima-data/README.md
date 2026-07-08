# anbima-data

ANBIMA data-feed skills. Every skill can hit ANBIMA two ways:

- `source="agnes"` (default) — via the internal Agnes API server, which handles the ANBIMA OAuth token dance on the backend. Requires `AGNES_API_KEY` (Agnes uses HTTPBearer on every `/api/v1/*` endpoint).
- `source="anbima"` — directly against `api.anbima.com.br`, using OAuth client-credentials with a locally-cached token. Requires `ANBIMA_CLIENT_ID` / `ANBIMA_CLIENT_SECRET` env vars.

## Skills

| Skill | Endpoint (Agnes) | Purpose |
|---|---|---|
| `anbima-funds-data` | `GET /api/v1/anbima/cadastral-data/{codigo}` | Cadastral data for a Brazilian investment fund by ANBIMA code (fund-only) |
| `anbima-funds-historical-data` | `GET /api/v1/anbima/historical-data/{codigo}` | Fund historical series (PL, cota, rentabilidade; optional `size`, `data_inicio`) |
| `anbima-cri-cra-market` | `GET /api/v1/anbima/cri-cra-secondary-market` | CRI/CRA secondary-market snapshot (optional `data_referencia`) |

## Setup

```bash
cd plugins/anbima-data
cp .env.example .env
# edit .env with real credentials (only needed for source=anbima)
```

Dependencies: `httpx`, `python-dotenv` (assumed present in the Claude Code Python environment; see `shared/requirements.txt`).

## Usage (direct, without Claude)

```bash
python skills/anbima-funds-data/scripts/fetch_funds_data.py --codigo 258.363 --source agnes
python skills/anbima-funds-historical-data/scripts/fetch_funds_historical_data.py --codigo C0000751243 --size 30 --source agnes
python skills/anbima-cri-cra-market/scripts/fetch_cri_cra.py --data-referencia 2026-07-01 --source agnes
```

## Direct-ANBIMA mode

`source="anbima"` on the data endpoints is stubbed until the raw ANBIMA URLs are pasted into `shared/_anbima_client.py` (constants `ANBIMA_CADASTRAL_URL`, `ANBIMA_HISTORICAL_URL`, `ANBIMA_CRI_CRA_URL`). The token flow is fully wired; only the three data URLs need to come from the reference `AnbimaAPIClient` code. Until then the client raises `NotImplementedError` with a pointer.
