---
name: anbima-funds-historical-data
description: "Use when the user wants the ANBIMA historical series for a Brazilian investment fund by ANBIMA code — série histórica com valor_cota, valor_patrimonio_liquido, valor_volume_total_aplicacoes / resgates, numero_cotistas, data_competencia (PT: 'puxa histórico ANBIMA do fundo X', 'série histórica ANBIMA do fundo Y', 'cotas históricas do fundo'; EN: 'get ANBIMA fund historical for code Z', 'fetch ANBIMA time series for fund'). Supports optional start date and row cap. Fund-scoped only — do not use for CRI/CRA/debêntures. The skill calls the Ayunit MCP tool `get_anbima_historical_data`, which the Ayunit backend proxies to ANBIMA server-side. Cross-surface: works in Claude Code, Claude Desktop, and Claude Cowork."
---

# Fetch ANBIMA fund historical series via the Ayunit MCP

Fetches ANBIMA's daily time series for a single **Brazilian investment fund**:
`valor_cota`, `valor_patrimonio_liquido`, subscription/redemption volumes,
number of quotaholders, currency, and `data_competencia`. Useful for
backfilling `AssetData` price history or spot-checking a fund's published
NAV/cota against our book. Pair with `anbima-funds-data` when you need both
registry metadata AND the NAV series.

## Inputs

- **anbima_code** (required) — the fund's ANBIMA `codigo_classe` (e.g.
  `C0000000191`) or, for multiclasse funds, the `codigo_subclasse` (e.g.
  `S0000730300`). **Note**: for a fund with subclasses the série histórica
  lives at the subclasse level — pass the `S…` code when one exists. **Not**
  the CNPJ, **not** the `codigo_fundo` (`F…`).
- **data_inicio** (optional, `YYYY-MM-DD`) — earliest date to include. When
  omitted, the ANBIMA backend returns from its own earliest date.
- **size** (optional int, default 1000) — max rows to return.

## How to invoke

Call the MCP tool directly:

```
mcp__ayunit__get_anbima_historical_data(
    anbima_code="C0000751243",
    data_inicio="2026-01-01",
    size=30,
)
```

The tool returns the raw ANBIMA JSON. No Python, no credentials, no `.env`
— the Ayunit backend holds the ANBIMA client-id/secret and handles the
OAuth token dance server-side.

## Errors surface upstream

- `403 Acesso negado` → the code isn't in ANBIMA's subscription for this
  environment, or you passed a `codigo_fundo` (`F…`) instead of a
  `codigo_classe`/`codigo_subclasse`. Verify the identifier and retry.
- `404` → code doesn't exist. Double-check with the user.
- Any MCP-level error → surface verbatim; don't retry silently.
