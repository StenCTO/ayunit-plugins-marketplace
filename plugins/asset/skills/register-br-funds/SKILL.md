---
name: register-br-funds
description: Use when the user wants to register a NEW Brazilian fund (Fundos, FII, FIDC, FIP, Previdência) in Global.Asset and only has the CNPJ — typically said as "cadastra esse fundo", "registra esse FII", "tenho só o CNPJ do fundo X". Resolves CNPJ → ANBIMA classe/subclasse code via AssetDataDB.Routines.UnitData (unit `fundos_anbima_dados_cadastrais`), then calls the ayunit `get_anbima_cadastral_data` tool to pull the full ANBIMA registry (denomination, administrator, manager, taxonomy, inception date), and hands off a pre-filled payload to the `asset-register` skill for duplicate-check, peer classification, preview, and INSERT. Does NOT insert into the database itself — `asset-register` owns the write. If the user already has the ANBIMA code (no CNPJ lookup needed) start at step 2.
---

# Cadastrar um fundo brasileiro a partir do CNPJ

Você é o especialista em **resolver os dados cadastrais de um fundo brasileiro** a partir do CNPJ
para alimentar o registro em `Global.Asset`. Esta skill é a **fonte de campos crus + identificadores**
que o `asset-register` consome — ela não classifica por peers (`AssetGroup`/`Product`/`AssetClass`),
não checa duplicata, não escreve no banco. Faz duas coisas, bem feitas:

1. CNPJ → código ANBIMA (classe e/ou subclasse) via `AssetDataDB.Routines.UnitData`.
2. Código ANBIMA → cadastral ANBIMA completo via `get_anbima_cadastral_data`.

E entrega o payload mapeado para o `asset-register`, que faz o duplicate-check + peer-analogy +
preview + INSERT via `Global.Asset_Update @CMD='I'`.

## Inputs

Um (ou mais) **CNPJ** do fundo — com ou sem pontuação. Aceite também:

- **Código ANBIMA já em mãos** (`C0000000191` classe, `S0000730300` subclasse) → pule o passo 1.
- **Lote** de CNPJs (ex: registrar uma família de classes/subclasses) → resolva todos no mesmo
  passo 1, depois itere passo 2+entrega por linha.

Normalize CNPJ tirando pontuação (`49.844.718/0001-53` → `49844718000153`) antes de consultar.
Sempre **eco os CNPJs normalizados** no topo da resposta e responda no idioma do usuário (PT/EN).

## Fluxo

### 1 — CNPJ → código ANBIMA (`execute_select_query` em `AssetDataDB`)

A unit `fundos_anbima_dados_cadastrais` é um snapshot JSON com uma linha por classe/subclasse de
fundo. O CNPJ pode bater com qualquer um dos três níveis (`identificador_fundo`,
`identificador_classe`, `identificador_subclasse`) — o LEFT JOIN abaixo cobre os três:

```sql
WITH latest AS (
    SELECT TOP 1 ValueJson
    FROM AssetDataDB.Routines.UnitData
    WHERE Unit = 'fundos_anbima_dados_cadastrais'
    ORDER BY InputDate DESC
),
cnpjs AS (
    SELECT cnpj FROM (VALUES
        ('<cnpj1>'),('<cnpj2>')         -- normalizados, sem pontuação
    ) v(cnpj)
),
funds AS (
    SELECT
        j.codigo_fundo, j.identificador_fundo,
        j.codigo_classe, j.identificador_classe,
        NULLIF(j.codigo_subclasse,'')        AS codigo_subclasse,
        NULLIF(j.identificador_subclasse,'') AS identificador_subclasse,
        j.razao_social_classe
    FROM latest
    CROSS APPLY OPENJSON(latest.ValueJson, '$.FundosAnbimaDadosCadastrais')
    WITH (
        codigo_fundo            NVARCHAR(50)  '$.codigo_fundo',
        identificador_fundo     NVARCHAR(20)  '$.identificador_fundo',
        codigo_classe           NVARCHAR(50)  '$.codigo_classe',
        identificador_classe    NVARCHAR(20)  '$.identificador_classe',
        codigo_subclasse        NVARCHAR(50)  '$.codigo_subclasse',
        identificador_subclasse NVARCHAR(20)  '$.identificador_subclasse',
        razao_social_classe     NVARCHAR(300) '$.razao_social_classe'
    ) j
)
SELECT
    c.cnpj,
    f.codigo_fundo,
    f.codigo_classe,
    f.codigo_subclasse,
    f.razao_social_classe
FROM cnpjs c
LEFT JOIN funds f
    ON c.cnpj IN (f.identificador_fundo, f.identificador_classe, f.identificador_subclasse)
ORDER BY c.cnpj, f.codigo_classe, f.codigo_subclasse;
```

Chamada via MCP: `execute_select_query(database='AssetDataDB', query=<SQL acima>)`.

**Como ler o resultado:**

- **Linha única, `codigo_subclasse` preenchido** → use esse subclasse code no passo 2; o fundo tem
  subclasses e o CNPJ é dessa subclasse específica.
- **Linha única, `codigo_subclasse` NULL** → use `codigo_classe`; o fundo é classe única (ou o CNPJ é
  o da classe).
- **Múltiplas linhas para o mesmo CNPJ** → fundo com várias subclasses; mostre todas e **pergunte
  qual subclasse o usuário quer cadastrar**. Cada subclasse vira uma linha distinta em `Global.Asset`.
- **Nenhuma linha (`codigo_classe` NULL)** → CNPJ não está na unit. Possíveis causas: fundo offshore,
  fundo recém-lançado fora do snapshot, ou CNPJ errado. **Pare e avise o usuário** — sem código ANBIMA
  não dá pra seguir esse fluxo; ofereça `asset-register` direto (cadastro 100% manual) como alternativa.

### 2 — Código ANBIMA → cadastral ANBIMA (`get_anbima_cadastral_data`)

Chame o MCP com o código resolvido (subclasse se houver, classe se não):

```
get_anbima_cadastral_data(anbima_code='<C0000000191 | S0000730300>')
```

A tool aceita ambos os formatos. O retorno traz a denominação oficial, administrador, gestor, CNPJ,
categoria/subcategoria ANBIMA, data de início e demais atributos do registro ANBIMA.

**O que extrair** (campos típicos — confirme os nomes na resposta real, podem variar entre classe e
subclasse):

| Campo ANBIMA | Vai para `Global.Asset_Update` como | Notas |
|---|---|---|
| `nome_comercial_classe` / `razao_social_classe` | `Description` | nome oficial; respeite acentuação. Prefira `nome_comercial_*` (mais curto, cabe em `nvarchar(100)`). |
| `codigo_fundo` (F-prefix) | `FundCode` | **sempre preencha** — é o identificador do "fundo" pai, comum entre classes/subclasses irmãs. Convenção da casa (confirmada vivendo em `Global.v_Asset`). |
| `codigo_classe` (C-prefix) | `ClassCode` | sempre preencha |
| `codigo_classe` (C-prefix) ou `codigo_subclasse` (S-prefix) | `AnbimaCode` **e** `Asset` | use o **nível mais específico** que cadastrou — S se houver subclasse, C se monoclasse. Convenção: `Asset` (código natural UNIQUE) = mesmo valor do `AnbimaCode`. |
| `identificador_classe` (CNPJ) | `Cnpj` | normalizado, sem pontuação |
| `nivel1_categoria` / `nivel2_categoria` / `nivel3_subcategoria` / `tipo_anbima` | **hint** para `Product`/`AssetClass` | NÃO grave direto — vira sugestão pro peer-match no `asset-register` |
| `indices_benchmark[].benchmark` | **hint** para `Benchmark` | `CDI`→`CDI`, `IBOVESPA`→`IBOV`, `OUTROS`→peer default (geralmente `IBOV` em ações); valide contra peers |
| `tributacao_perseguida` | **hint** para `TaxRegime` | "Longo Prazo"→`Longo Prazo`; equity ("Alíquota de 15%") geralmente cai em `null` por convenção de peers |
| `valores_minimos_movimentacao_classe.prazo_emissao_cota` | `SubscriptionDaysToQuote` | D+x da subscrição |
| `valores_minimos_movimentacao_classe.prazo_conversao_resgate` | `RedemptionDaysToQuote` | D+x da conversão (cota) |
| `valores_minimos_movimentacao_classe.prazo_pagamento_resgate` | `RedemptionDaysToSettle` | D+x do pagamento (liquidação) |
| `data_inicio_atividade_classe` | informativo | `Global.Asset` não tem coluna de inception; apenas registre na conversa |

> ⚠️ A categoria/subcategoria ANBIMA **não bate 1:1** com `Product`/`AssetClass` da casa
> (taxonomia interna). Trate como **hint** e deixe o `asset-register` validar contra peers reais
> em `Global.v_Asset`.

### 3 — Montar o payload de entrega para `asset-register`

Sempre preencha o que já temos com certeza; deixe a classificação (`AssetGroup`, `SecurityType`,
`Product`, `AssetClass`, `Benchmark`, `Source`, `TaxRegime`) **vazia** para o `asset-register`
resolver por analogia com peers vivos no banco. Sugestão de payload (JSON):

```json
{
  "source": "register-br-funds",
  "Asset": "<código mais específico — S se subclasse, C se monoclasse>",
  "Description": "<nome_comercial_classe>",
  "Cnpj": "<identificador_classe normalizado>",
  "AnbimaCode": "<= Asset>",
  "ClassCode": "<codigo_classe (C-prefix)>",
  "FundCode": "<codigo_fundo (F-prefix)>",
  "Currency": "BRL",
  "Offshore": 0,
  "Activated": 1,
  "ContractSize": 1,
  "SubscriptionDaysToQuote": "<prazo_emissao_cota>",
  "RedemptionDaysToQuote":   "<prazo_conversao_resgate>",
  "RedemptionDaysToSettle":  "<prazo_pagamento_resgate>",
  "hints": {
    "anbima_categoria": "<nivel1_categoria>",
    "anbima_subcategoria": "<nivel2_categoria + nivel3_subcategoria>",
    "anbima_tipo": "<tipo_anbima>",
    "anbima_benchmark": "<indices_benchmark[0].benchmark>",
    "anbima_tributacao": "<tributacao_perseguida>",
    "tipo_indicado": "Fundos | FII | FIDC | FIP | Previdência"
  }
}
```

Notas:

- **`Asset` (código natural UNIQUE)** — convenção da casa: usar **o próprio código ANBIMA** (`C…`
  para monoclasse, `S…` para subclasse). Para FIIs listados (ex: `KNRI11`) o ticker B3 também é
  aceito — confirme com o usuário se preferir. Não há exigência de código curto.
- **Hints**, não verdade**.** Categoria ANBIMA é dica para o `asset-register` buscar peers do mesmo
  tipo — a classificação final sai do GROUP BY de peers reais (ver `asset-register` §2).
- Se a query do passo 1 trouxe **múltiplas subclasses** e o usuário quer cadastrar todas, gere um
  payload por subclasse e entregue como lote — o `asset-register` suporta canary-then-loop.

### 4 — Hand-off ao `asset-register`

Você não escreve no banco. Mostre o payload ao usuário e diga explicitamente:

> "Payload pronto. Encaminhando para `asset-register` — ele vai (a) checar duplicata por
> CNPJ/AnbimaCode/Asset, (b) buscar peers do mesmo tipo no `Global.v_Asset` para classificar, (c)
> apresentar a linha completa e pedir confirmação, (d) inserir via `Global.Asset_Update @CMD='I'` e
> verificar."

O `asset-register` é quem cuida do duplicate-gate, do peer-analogy, do preview-e-confirma e do
INSERT/verify. Não tente atalho.

## Critical rules

- **Nunca insira** em `Global.Asset` por esta skill. O write path é exclusivo do `asset-register`.
- **Nunca invente** o `AnbimaCode`, `ClassCode`, `FundCode` ou a denominação — todos vêm da unit
  ANBIMA + `get_anbima_cadastral_data`. Se a unit não bater o CNPJ, pare.
- **Nunca grave** a categoria/subcategoria ANBIMA direto em `Product`/`AssetClass` — é hint para o
  peer-match, não verdade.
- **Snapshot é diário.** A unit `fundos_anbima_dados_cadastrais` é o snapshot mais recente
  (`ORDER BY InputDate DESC`). Um fundo registrado na ANBIMA hoje só aparece na próxima ingestão.
  Se o usuário diz "saiu hoje", confirme a data do snapshot (`MAX(InputDate)`) antes de declarar
  ausência.
- **CNPJ é a chave de busca**, não o `Asset`. Vários `Global.Asset` podem dividir o mesmo CNPJ
  (classe vs subclasses) — o `asset-register` decide se é duplicata olhando `AnbimaCode`, não só
  CNPJ.
- **Reply in the user's language** (PT/EN) e ecoe os CNPJs normalizados + código ANBIMA resolvido no
  topo de cada resposta.

## When unsure

- **CNPJ não aparece na unit** → confirme `MAX(InputDate)` da unit; se o snapshot é antigo, peça
  para o usuário aguardar a próxima ingestão ou fornecer o código ANBIMA manualmente. Se for fundo
  offshore (não-BR), esta skill não se aplica — encaminhe para `asset-register` direto.
- **Múltiplas subclasses para o mesmo CNPJ** → liste todas (`codigo_subclasse`, `razao_social_classe`)
  e pergunte qual cadastrar. Não escolha por conta própria.
- **`get_anbima_cadastral_data` retorna erro / 404** → o código existe na unit mas a API ANBIMA do
  Ayunit não tem ele. Pare e mostre a resposta literal ao usuário; pode ser código stale na unit.
- **Usuário não tem o `Asset` code** (ticker / código natural) → pergunte antes do hand-off. O
  `asset-register` exige `Asset` (UNIQUE NOT NULL).
- **Usuário pede para atualizar/soft-deletar** em vez de registrar → não é o escopo desta skill;
  encaminhe para o `ayunit_asset` prompt.
