"""Shared ANBIMA data client for the anbima-data plugin.

Two sources per data method:
- source="agnes" (default): calls the internal Agnes API server GET auto-token
  variants. Agnes handles the OAuth on the backend.
- source="anbima": calls api.anbima.com.br directly with an OAuth
  client-credentials token cached locally. The three data-endpoint URL
  constants below (ANBIMA_*_URL) must be filled in from the reference
  AnbimaAPIClient before source="anbima" works for the data methods.
  The token flow itself IS fully implemented.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_a, **_kw):
        return False


ANBIMA_TOKEN_URL = "https://api.anbima.com.br/oauth/access-token"

# Direct ANBIMA URLs. All /feed/fundos/v2/* endpoints take the FUND identifier
# as {codigo} — specifically the `codigo_classe` (Cxxxxxxxxxx) or, for
# multiclasse funds, the `codigo_subclasse`. It is NOT the CNPJ, NOT the raw
# ANBIMA code like 258.363, and NOT the codigo_fundo (Fxxxxxxxxxx).
# Auth for these endpoints is added by _anbima_direct_headers() → sends
# `client_id` + `access_token` headers, where access_token is returned by
# _get_anbima_token() (cache-first: reuses %TEMP%/anbima_token.json until
# 60s before expiry, only hits /oauth/access-token when the cache misses).
ANBIMA_CADASTRAL_URL: str | None = "https://api.anbima.com.br/feed/fundos/v2/fundos/{codigo}"
ANBIMA_HISTORICAL_URL: str | None = "https://api.anbima.com.br/feed/fundos/v2/fundos/{codigo}/serie-historica"
ANBIMA_CRI_CRA_URL: str | None = "https://api.anbima.com.br/feed/precos-indices/v1/cri-cra/mercado-secundario"

DEFAULT_AGNES_BASE = "http://agnes.brazilsouth.cloudapp.azure.com:8000"
HTTP_TIMEOUT = 30.0

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_ENV_LOADED = False


def _load_env() -> None:
    """Load credentials. Real OS environment variables are the source of truth.

    Precedence (highest wins, never overridden):
      1. Real OS environment variables (Windows `setx`, Unix `export`).
         python-dotenv is called with override=False, so anything already
         present in os.environ is never touched. This is the intended
         production path for Claude Desktop / marketplace installs.
      2. Plugin-local `.env` at `<plugin-root>/.env`  — dev workflow when
         editing this repo. Gitignored.
      3. User-level `.env` at `~/.anbima-data.env`   — fallback for machines
         where OS env vars aren't set and the plugin folder is read-only.

    Missing files are ignored silently. Steps 2/3 fill in only what step 1
    left unset.
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    load_dotenv(_PLUGIN_ROOT / ".env", override=False)
    load_dotenv(Path.home() / ".anbima-data.env", override=False)
    _ENV_LOADED = True


def _agnes_base() -> str:
    _load_env()
    return os.environ.get("AGNES_BASE_URL", DEFAULT_AGNES_BASE).rstrip("/")


def _agnes_headers() -> dict[str, str]:
    _load_env()
    token = os.environ.get("AGNES_API_KEY")
    if not token:
        raise RuntimeError(
            "Missing AGNES_API_KEY. Set it in the environment or in "
            "plugins/anbima-data/.env (copy .env.example). Agnes uses "
            "HTTPBearer on every /api/v1 endpoint."
        )
    return {"Authorization": f"Bearer {token}"}


def _token_cache_path() -> Path:
    return Path(tempfile.gettempdir()) / "anbima_token.json"


def _read_cached_token() -> str | None:
    p = _token_cache_path()
    if not p.exists():
        return None
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
        expires_at = datetime.fromisoformat(blob["expires_at"])
        if expires_at > datetime.now(timezone.utc) + timedelta(seconds=60):
            return blob["access_token"]
    except Exception:
        return None
    return None


def _write_cached_token(access_token: str, expires_in: int) -> None:
    p = _token_cache_path()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    p.write_text(
        json.dumps({"access_token": access_token, "expires_at": expires_at.isoformat()}),
        encoding="utf-8",
    )


def _get_anbima_token() -> str:
    cached = _read_cached_token()
    if cached:
        return cached

    _load_env()
    client_id = os.environ.get("ANBIMA_CLIENT_ID")
    client_secret = os.environ.get("ANBIMA_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "Missing ANBIMA_CLIENT_ID / ANBIMA_CLIENT_SECRET. "
            "Set them in the environment or in plugins/anbima-data/.env "
            "(copy .env.example). Not needed for source='agnes'."
        )

    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        r = client.post(
            ANBIMA_TOKEN_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {creds}",
            },
            json={"grant_type": "client_credentials"},
        )
    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"ANBIMA token request failed: HTTP {r.status_code} — {r.text}"
        )
    data = r.json()
    access_token = data["access_token"]
    expires_in = int(data.get("expires_in", 3600))
    _write_cached_token(access_token, expires_in)
    return access_token


def _http_get(url: str, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None) -> Any:
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        r = client.get(url, headers=headers or {}, params=params or {})
    if r.status_code != 200:
        raise RuntimeError(f"GET {url} failed: HTTP {r.status_code} — {r.text}")
    return r.json()


def _anbima_direct_headers() -> dict[str, str]:
    """Headers for direct api.anbima.com.br calls (Sensedia gateway).

    ANBIMA's Sensedia gateway (verified 2026-07-08) requires TWO plain
    headers on every data call, NOT the usual `Authorization: Bearer`:
      - `client_id`: the OAuth client id
      - `access_token`: the JWT from the /oauth/access-token endpoint
    Anything else returns 401 with a Sensedia-specific hint.
    """
    _load_env()
    client_id = os.environ.get("ANBIMA_CLIENT_ID")
    if not client_id:
        raise RuntimeError("Missing ANBIMA_CLIENT_ID (needed as HEADER by ANBIMA's Sensedia gateway).")
    return {
        "client_id": client_id,
        "access_token": _get_anbima_token(),
    }


def _anbima_direct_not_ready(constant_name: str) -> None:
    raise NotImplementedError(
        f"source='anbima' requires the {constant_name} constant in "
        f"plugins/anbima-data/shared/_anbima_client.py to be filled in "
        f"from the reference AnbimaAPIClient. Use source='agnes' meanwhile."
    )


def get_cadastral(codigo: str, source: str = "agnes") -> dict:
    if source == "agnes":
        url = f"{_agnes_base()}/api/v1/anbima/cadastral-data/{codigo}"
        return _http_get(url, headers=_agnes_headers())
    if source == "anbima":
        if not ANBIMA_CADASTRAL_URL:
            _anbima_direct_not_ready("ANBIMA_CADASTRAL_URL")
        return _http_get(
            ANBIMA_CADASTRAL_URL.format(codigo=codigo),
            headers=_anbima_direct_headers(),
        )
    raise ValueError(f"Unknown source: {source!r} (expected 'agnes' or 'anbima')")


def get_historical(
    codigo: str,
    size: int | None = None,
    data_inicio: str | None = None,
    source: str = "agnes",
) -> dict:
    if source == "agnes":
        params: dict[str, Any] = {}
        if size is not None:
            params["size"] = size
        if data_inicio:
            params["data_inicio"] = data_inicio
        url = f"{_agnes_base()}/api/v1/anbima/historical-data/{codigo}"
        return _http_get(url, headers=_agnes_headers(), params=params)
    if source == "anbima":
        if not ANBIMA_HISTORICAL_URL:
            _anbima_direct_not_ready("ANBIMA_HISTORICAL_URL")
        # ANBIMA's direct endpoint spells this param with a HYPHEN, not underscore.
        params = {}
        if size is not None:
            params["size"] = size
        if data_inicio:
            params["data-inicio"] = data_inicio
        return _http_get(
            ANBIMA_HISTORICAL_URL.format(codigo=codigo),
            headers=_anbima_direct_headers(),
            params=params,
        )
    raise ValueError(f"Unknown source: {source!r} (expected 'agnes' or 'anbima')")


def get_cri_cra(data_referencia: str | None = None, source: str = "agnes") -> dict:
    params: dict[str, Any] = {}
    if data_referencia:
        params["data_referencia"] = data_referencia

    if source == "agnes":
        url = f"{_agnes_base()}/api/v1/anbima/cri-cra-secondary-market"
        return _http_get(url, headers=_agnes_headers(), params=params)
    if source == "anbima":
        if not ANBIMA_CRI_CRA_URL:
            _anbima_direct_not_ready("ANBIMA_CRI_CRA_URL")
        return _http_get(
            ANBIMA_CRI_CRA_URL,
            headers=_anbima_direct_headers(),
            params=params,
        )
    raise ValueError(f"Unknown source: {source!r} (expected 'agnes' or 'anbima')")
