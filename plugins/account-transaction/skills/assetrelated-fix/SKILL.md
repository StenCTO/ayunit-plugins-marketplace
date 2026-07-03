---
name: assetrelated-fix
description: "Use when the user wants to fix / fill the AssetRelated of income transactions in Portfolio.AccountTransaction — GENERAL LEDGER RECEIPT rows with GeneralLedgerType='INTEREST/DIVIDEND' (coupons, dividends, rendimentos, fee rebates, JP bond coupons) whose AssetRelated is unresolved, so the cash flow disappears from asset return attribution (it never ties to an Asset in AccountPosition). Covers BOTH the same defect's faces: VALIDATED rows left with AssetRelated NULL (scenario 1) and PENDING income rows that escaped the routines and stay out of AccountPosition (scenario 2). The skill scopes the defect by any AccountTransaction filter, parses the originating security out of the custody description via three layouts — XP layout A ('RENDIMENTOS DE CLIENTES <ticker>'), XP layout B ('Devolução Tx de Distr <fund>'), and JP/international layout C (ISIN embedded in the description OR CUSIP carried out-of-band in RawTransaction.Cusip) — CONFIRMS the match (A/B against the account's holding universe; C against a global ISIN/CUSIP index), and — fully autonomously for high-conviction, lock-aware matches — sets AssetRelated and promotes the row to UPDATED via Portfolio.AccountTransaction_Update @CMD='U'. Everything it cannot prove is reported for a human. Trigger whenever the user says income/dividends/juros/rendimentos/coupons aren't attributed to an asset, AssetRelated is missing/null/inconsistent, or income/GL-RECEIPT trades are stuck PENDING (PT or EN). Sibling of compromissada-fix and duplicate-trade-reconcile."
---

# Link & rescue income receipts — fill `AssetRelated`, promote stuck `PENDING`

You are the orchestrator for **attributing income to its asset** in
`Portfolio.AccountTransaction`. A `GENERAL LEDGER RECEIPT` with
`GeneralLedgerType = 'INTEREST/DIVIDEND'` (a coupon, dividend, *rendimento*, or fee
rebate) carries `Asset = 'BRL'` and names the **paying security** in `AssetRelated`.
When the loader can't resolve that security, the receipt's cash still lands but it is
**orphaned from return attribution** — it never ties to an `Asset` in
`AccountPosition`, so the asset's income silently vanishes from its return.

This one defect wears two faces, and this skill fixes **both**:

- **Scenario 1 — `VALIDATED`, `AssetRelated` NULL.** The row counts as cash but is
  unattributed (e.g. XP `RENDIMENTOS DE CLIENTES <ticker>` rows the loader validated
  yet sometimes left unlinked — pk 81970 got `CPTI11`, its twin pk 81973 did not).
- **Scenario 2 — `PENDING`, escaped the routines.** The same income, but the loader
  left it `PENDING` (so it doesn't reach `AccountPosition` at all) because it couldn't
  pin the asset — e.g. XP `Devolução Tx de Distr <fund>` rows. The treatment is always
  the same: resolve `AssetRelated`, then promote.

The fix is identical for both: **resolve `AssetRelated` from the description, confirm
it is a security the account actually holds, write it, and set `Status = 'UPDATED'`.**
`Asset`, `Quantity`, `Price`, `Value` never change — only `AssetRelated` (and `Status`).

This is a **self-contained orchestration skill** built to run **autonomously**: it
auto-commits the matches it can prove with **high conviction** (lock permitting) and
**reports** everything else for a human. It is **non-destructive** — every write is a
`@CMD='U'` that adds an attribution and an audit note; it never deletes or moves cash.

## Coherence is the proof — with one carve-out

The ground truth for "which asset paid this?" is **what the account holds** — for text
matches (layouts A and B). Never set `AssetRelated` to a security the account doesn't
hold or hasn't traded when the only evidence is a ticker or a fund name — a wrong link
silently mis-attributes the income. So for A/B, every candidate the text suggests is
confirmed against the account's **holding universe** (positions in `v_AccountPosition` ∪
assets ever traded in `v_AccountTransaction`). Text alone is a hint; a held match is proof.

**Layout C is the exception.** An ISIN or a custody CUSIP is a **globally unique
identifier** — a match against the registered `Global.Asset` / `Portfolio.AssetCustody`
index *is* the proof of identity, on its own. Requiring the account to already hold the
asset would silently block legitimate coupon income whenever the position feed lags the
income feed (real case on JP bonds: confirmed AssetCustody/Isin match, zero rows in
`AccountPosition` / `AccountTransaction` / `CustodyPosition` for that account). So for
Layout C the coherence gate is **waived by design**; the identifier index replaces it.

## Inputs

Any filter over `Portfolio.v_AccountTransaction` — one or **many** accounts, `Custody`,
a `Date`/`SettlementDate` window, `Status`, or an explicit `pk_AccountTransactionID`
list. **Default scope:** `TransactionType = 'GENERAL LEDGER RECEIPT'`,
`GeneralLedgerType = 'INTEREST/DIVIDEND'`, `AssetRelated IS NULL`,
`Status IN ('PENDING','VALIDATED')`. Echo the resolved scope at the start of every report.

- Resolve a short account number against what's stored (XP onshore keys are **not**
  zero-padded — `4789186` stays `'4789186'`): `SELECT DISTINCT ClientAccount, Custody
  FROM Portfolio.v_AccountTransaction WHERE ClientAccount LIKE '%4789186%'`. Use the
  exact stored string thereafter.
- If the user passes explicit pks, scope to exactly those; otherwise discover by filter.
- **When the scope includes a custody that uses identifier-based income (JP, and any other
  international feed carrying ISIN/CUSIP)**, also pull the CUSIP off `RawTransaction` per
  row and echo it into the candidate's `cusip` field — layout C uses it. The description
  free-text sometimes carries the ISIN and sometimes doesn't; `RawTransaction.Cusip` is
  the reliable per-row identifier.

## Reference resources (read on demand)

| Resource | Read when… |
|---|---|
| [`ayunit://docs/transaction/fixes`](ayunit://docs/transaction/fixes) | **First.** Recipes **R3** and **R7** are the exact AssetRelated-link fix (R7 = the description-driven, account-confirmed link this skill automates); **R1** = completing a stuck `PENDING`. Plus the universal write guardrails (SELECT-first-merge, drop `AccountCurrency`/`AccountFx`, absolute values, `AgentCheck`). |
| [`ayunit://docs/checkeddate/usage`](ayunit://docs/checkeddate/usage) | **Before any write.** The lock contract: the proc rejects a write when an `Activated=1` `v_CheckedDate` exists for `(Account, Custody)` and the row's `Date` **or** `SettlementDate` ≤ the lock date. |
| [`ayunit://docs/transaction/types`](ayunit://docs/transaction/types) · [`procedure`](ayunit://docs/transaction/procedure) | `GENERAL LEDGER RECEIPT` sign rules (`Asset = Currency`, `AssetRelated` = originating security; Quantity/Price/Value all +), the `Status` lifecycle, and `AccountTransaction_Update` params. |
| [`ayunit://docs/portfolio-creator/pipeline`](ayunit://docs/portfolio-creator/pipeline) | Why an unattributed/`PENDING` receipt drops out of attribution: only `VALIDATED`/`UPDATED` rows with a resolved asset reach `AccountPosition`. |

## Tools you call directly

- `execute_select_query` — every read (scope, holding universe, lock lookup, verification).
- `scripts/resolve_assetrelated.py` — the deterministic matching engine (run via `bash`):
  parses the description, confirms against the holding universe, emits a per-row verdict
  (`HIGH` / `REPORT`). It never touches the DB.
- `execute_procedure(procedure='Portfolio.AccountTransaction_Update', cmd='U', …)` — the
  write path (one row at a time, SELECT-first-merge), **or** `execute_batch(items=[…
  cmd='U' …], dry_run=…)` for an atomic multi-row commit. `@CMD='U'` is non-destructive,
  so `allow_destructive` is not required.
- `get_view_detail` / `get_procedure_detail` — confirm columns/params; never guess.

## The correction cycle

### 1 — Scope the defect

Pull the candidate universe with every field you'll reason about. Read from the **view**.

```sql
SELECT pk_AccountTransactionID, Date, SettlementDate, ClientAccount, Custody,
       TransactionType, GeneralLedgerType, Asset, AssetRelated,
       COALESCE(GeneralLedgerDescription, Obs) AS Descr,
       Quantity, Price, Value, Currency, Status
FROM Portfolio.v_AccountTransaction
WHERE ClientAccount IN (…)                         -- or pk_AccountTransactionID IN (…)
  AND TransactionType = 'GENERAL LEDGER RECEIPT'
  AND GeneralLedgerType = 'INTEREST/DIVIDEND'
  AND AssetRelated IS NULL
  AND Status IN ('PENDING','VALIDATED')
ORDER BY ClientAccount, Date;
```

> **Stay in the income family.** Only `GENERAL LEDGER RECEIPT` / `INTEREST/DIVIDEND`
> with `AssetRelated IS NULL`. Do **not** widen to BUY/SELL/ASSET, to other
> `GeneralLedgerType`s, or to receipts that already carry an `AssetRelated`.

### 2 — Build the account's holding universe (the coherence set)

Per account in scope, the assets it holds **or** has ever traded, with their names:

```sql
WITH held AS (
  SELECT DISTINCT p.Asset, a.Description
  FROM Portfolio.v_AccountPosition p JOIN Global.v_Asset a ON a.Asset = p.Asset
  WHERE p.Account = '<acct>'
), traded AS (
  SELECT DISTINCT t.Asset, a.Description
  FROM Portfolio.v_AccountTransaction t JOIN Global.v_Asset a ON a.Asset = t.Asset
  WHERE t.ClientAccount = '<acct>' AND t.Asset <> 'BRL'
        AND t.Status IN ('VALIDATED','UPDATED')
), traded_rel AS (
  SELECT DISTINCT t.AssetRelated AS Asset, a.Description
  FROM Portfolio.v_AccountTransaction t JOIN Global.v_Asset a ON a.Asset = t.AssetRelated
  WHERE t.ClientAccount = '<acct>' AND t.AssetRelated IS NOT NULL AND t.AssetRelated <> 'BRL'
        AND t.Status IN ('VALIDATED','UPDATED')
)
SELECT Asset, Description FROM held
UNION SELECT Asset, Description FROM traded
UNION SELECT Asset, Description FROM traded_rel;
```

`AssetRelated` will be one of these `Asset` codes — which for many funds is the book's
internal code or an ANBIMA `C00…` code, **not** a ticker. That's fine: the resolver
returns the held asset's `Asset` code, whatever form it takes.

### 3 — Resolve `AssetRelated` (run the matching engine)

Write the scope rows (`pk`, `description` = the `Descr` column, `status`, any echo
fields, and — for identifier-based custodies — `cusip` pulled from `RawTransaction`) to
`candidates.json`, the holding universe to `holdings.json`, and (when Layout C applies)
the **global identifier index** to `identifiers.json`. Build the identifier index once
per run, custody-scoped:

```sql
-- swap 'JP' for whichever identifier-based custody is in scope
SELECT a.Asset, a.Isin, a.Cusip,
       ac.TickerCustody  AS custodyTicker,
       ac.TickerCustody2 AS custodyTicker2
FROM Global.v_Asset a
LEFT JOIN Portfolio.v_AssetCustody ac ON ac.Asset = a.Asset AND ac.Custody = 'JP'
WHERE a.Isin IS NOT NULL OR a.Cusip IS NOT NULL
   OR ac.TickerCustody IS NOT NULL OR ac.TickerCustody2 IS NOT NULL;
```

Then:

```bash
python3 scripts/resolve_assetrelated.py candidates.json holdings.json identifiers.json > plan.json
```

(`identifiers.json` is optional; omit it when only XP layouts A/B are in scope.)

> **Identifier-index fetch strategy.** The unfiltered SQL above returns *every*
> asset with any ISIN/CUSIP/AssetCustody mapping — that easily blows past a
> 500-row page or the tool-result token cap on a mature book (verified
> 2026-07-03: >186 kB result on JP). Two safe fetch modes, pick by scope size:
>
> - **Small candidate set (< ~50 rows):** first collect the ISINs and CUSIPs
>   actually present in the candidate set (parse ISINs from `Descr` with the
>   Layout C regex; read CUSIPs off `RawTransaction`), then **filter the SQL
>   by those identifiers only** — replace the `WHERE ... IS NOT NULL` clause
>   with `WHERE a.Isin IN (…) OR a.Cusip IN (…) OR ac.TickerCustody IN (…) OR
>   ac.TickerCustody2 IN (…)`. The result is O(candidate rows), well under any
>   cap. This is the mode the resolver was validated against on JP.
> - **Large candidate set or bulk sweep:** paginate the unfiltered index by
>   `Asset` (`ORDER BY a.Asset OFFSET n ROWS FETCH NEXT 500`), concatenate the
>   pages into a single `identifiers.json`. Don't try to run without an index
>   — Layout C without identifiers is a silent no-op (every C row falls to
>   REPORT "no ISIN in text and no cusip field supplied" or "not registered").
>
> Either way, pull the index **fresh** per run — it's reference data, cheap to
> query, and stale index entries silently mis-attribute income.

The engine recognises three income layouts:

| Layout | Description grammar / signal | How the asset is found | HIGH when… |
|---|---|---|---|
| **A** | `RENDIMENTOS DE CLIENTES <TICKER> S/ <n>` | the **ticker** is literal in the text (`CPTI11`, `CDII11`) | the ticker **equals** exactly one **held** asset's `Asset` code/ticker |
| **B** | `Devolução Tx de Distr <FUND NAME>` | **fuzzy** match the fund name to a **held** asset's `Description` (accent/spacing/structure-word insensitive) | exactly one held asset scores ≥ 0.86 with a clear margin over the runner-up |
| **C** | an **ISIN** (`[A-Z]{2}[A-Z0-9]{9}[0-9]`) embedded in the description, **or** a **CUSIP** carried out-of-band via the candidate's `cusip` field (JP `RawTransaction.Cusip`) | exact match against the **global** identifier index — `Global.Asset.Isin` / `Cusip` **or** the custody-side `Portfolio.AssetCustody.TickerCustody` / `TickerCustody2` (JP sometimes books a different CUSIP variant than Global.Asset) | one and only one asset in the identifier index matches; **holding universe not required** (ISIN/CUSIP is identity, not a hint) |
| **SWEEP** | `DEPOSIT SWEEP INTEREST FOR <period> @ <rate> RATE ON AVG COLLECTED BALANCE OF $<amt>` (JP monthly cash-sweep interest) | recognition-only — there is no paying security; interest is earned on the USD balance itself | **never HIGH.** These rows are *loader mis-classified*: correct `GeneralLedgerType` is `'OVERNIGHT'` and `AssetRelated` stays NULL by design. Report and hand off to the loader / GL-type fix — reclassifying `GeneralLedgerType` is **out of this skill's write scope** (this skill only writes `AssetRelated` + `Status`) |

Everything else — unknown grammar, a security the account doesn't hold (A/B), an
identifier not registered in `Global.Asset` (C), or an ambiguous / conflicting match —
comes back `REPORT` (never auto-written). This is by design: the engine reproduces the
careful human link (it independently re-derives `SULAMEX BZ` for the `Devolução …
SulAmérica …` rows, and `BARCLPLC 4.5% 22JAN2026` from JP's Cusip `6M023A9D0`) and
refuses to guess when the evidence isn't clean.

> **Curated alias map (`scripts/alias_map.json`).** Resolution consults this file
> **before** the fuzzy step. It maps a normalised description-family (e.g.
> `JIVE BOSSANOVA HIGH YIELD ADVISORY FIC FIDC`) directly to an `Asset` code that a
> human has confirmed. An alias hit is treated as **human-confirmed HIGH** — it
> bypasses both the fuzzy threshold and the holding-universe coherence gate (the
> operator has vouched that this income name belongs to this asset). This is how
> recurring REPORT families (distribution-fee rebates from funds whose registered
> `Description` doesn't lexically match XP's marketing name) become auto-fixes. To
> extend it: confirm the `Asset` exists in `Global.Asset`, add a
> `{"name": "...", "asset": "C00..."}` entry, and re-run. Mining the REPORT bucket
> for new alias candidates is the natural way to grow this file.

> **Extending to new layouts/custodies.** The default grammars are XP's. BTG / MS income
> receipts use different description text — until their grammar is added to the resolver,
> those rows fall to `REPORT`. To add one: confirm the new grammar from real
> `RawTransaction`/`Descr` samples, add a parser + its `HIGH` rule to
> `resolve_assetrelated.py`, and re-test against already-fixed (`UPDATED`) rows of that
> family before trusting it.

### 4 — Lock-gate (CheckedDate)

Read the active locks once for the accounts in scope:

```sql
SELECT Account, Custody, Date, Activated
FROM Portfolio.v_CheckedDate
WHERE Account IN (…) AND Activated = 1 ORDER BY Account, Date DESC;
```

- Flag any `(Account, Custody)` with **>1** `Activated=1` row — the proc's scalar
  subquery raises (error 512) on duplicates, so *every* write on that account fails.
  Skip those accounts; report them.
- A row is **writable** only if `Date > lock AND SettlementDate > lock` (or no active
  lock). A `HIGH` match sitting **on/before** the lock is **lock-blocked**: report it,
  don't touch the lock (a lock move triggers the recoil cycle and is a user decision).

### 5 — Auto-commit the high-conviction matches, then verify

Fully autonomous for `HIGH` + writable rows — no canary, no pause:

1. **Write each fix — SELECT-first-merge.** Read the full current row, build params from
   **every populated column**, **drop `AccountCurrency` and `AccountFx`** (the proc
   computes them and rejects the whole payload otherwise), overlay just
   `AssetRelated = <matchedAsset>` and `Status = 'UPDATED'`, set `AgentCheck`, write
   `@CMD='U'`. `Asset` stays `BRL`; `Quantity`/`Price`/`Value` unchanged (a GL receipt is
   Quantity +, Price +, Value + — already correct). Pass **absolute** values.
   - `AgentCheck` format:
     `fix YYYY-MM-DD: GL RECEIPT linked - AssetRelated NULL-><asset> from description (layout <A|B|C>, <confirmed account holds it | identifier index match, no holding required>); Status <old>->UPDATED [AR]`
   - For a multi-row commit, build `execute_batch(items=[…], dry_run=true)` first, confirm
     `failed_index` is null, then `dry_run=false`. The batch is atomic.
2. **Verify sweep.** Re-SELECT every written pk: confirm `AssetRelated` is set to the held
   security, `Status = 'UPDATED'`, and `Asset`/`Value` unchanged. Re-run the §1 scope query
   and confirm the only `AssetRelated IS NULL` rows left are the ones you deliberately
   reported. If a row impacted attribution, note that the asset's income now ties through.

### 6 — Report buckets

End every run with these buckets so nothing is silently dropped:

| Bucket | Meaning | Disposition |
|---|---|---|
| **Linked** | `HIGH`, writable — `AssetRelated` set + promoted to `UPDATED` + verified | done |
| **Reported — unresolved** | no known layout, security not in the holding universe (A/B), ambiguous fuzzy match (B), identifier not registered in `Global.Asset` / `Portfolio.AssetCustody` (C), or ISIN vs CUSIP conflict (C) | **user decision**; show each row's `Descr`, the ISIN/CUSIP if applicable, the best candidate + score, and the holding set / identifier match attempt so the user can call it. A "not registered" C row is typically an `asset-register` job first, then re-run |
| **Reported — SWEEP (reclassify, don't link)** | JP `DEPOSIT SWEEP INTEREST` rows loader-mis-classified as `GeneralLedgerType='INTEREST/DIVIDEND'` | **not this skill's job.** The correct fix is at the loader / GL type: reclassify to `GeneralLedgerType='OVERNIGHT'` with `AssetRelated` NULL by design. This skill only writes `AssetRelated` + `Status`, so it recognises and reports these but never touches them. Count them separately from the generic unresolved bucket so the systemic loader issue is visible |
| **Lock-blocked** | a `HIGH` match whose `Date`/`SettlementDate` ≤ the account's active CheckedDate | needs a CheckedDate move (user-approved, audited, via `Portfolio.CheckedDate_Update` — not allowlisted; emit a copy-paste `EXEC`), then re-run |
| **Skipped — broken lock** | `(Account,Custody)` has >1 active CheckedDate | fix the duplicate lock first; report |

## Critical rules

- **Coherence is the proof — for layouts A and B.** Only ever set `AssetRelated` to a
  security the account **holds or has traded**. A name in the text that isn't in the
  holding universe → report, never write. **Layout C is exempt**: ISIN/CUSIP is a
  globally unique identifier, so a unique match in the identifier index (`Global.Asset` /
  `Portfolio.AssetCustody`) is proof on its own — the account does not need to already
  hold the asset. An identifier that resolves to zero assets, to multiple assets, or to
  conflicting assets (ISIN vs CUSIP disagree) → report.
- **High conviction only, autonomously.** Auto-write a match **only** when the resolver
  returns `HIGH` (unique exact ticker, or unique fuzzy ≥ threshold with margin) **and** the
  row is writable. Anything `REPORT` is for a human — don't guess.
- **Stay in the income family.** `GENERAL LEDGER RECEIPT` / `INTEREST/DIVIDEND` /
  `AssetRelated IS NULL` only. Don't re-type rows, don't touch other transaction types, and
  don't change `Asset`, `Quantity`, `Price`, or `Value` — only `AssetRelated` and `Status`.
- **Never write across a lock.** A match on/before the active CheckedDate is lock-blocked;
  the proc rejects it (and a batch rolls back). Gate yourself first.
- **SELECT-first-merge and drop `AccountCurrency`/`AccountFx`** before every write (else a
  generic 400 before SQL runs). Pass **absolute** values; the proc applies the sign.
- **Always set `AgentCheck`** with the `[AR]` tag so the next session can read the fix.
- **Verify after** every commit, and re-run the scope query to prove only intended rows remain.
- **Reply in the user's language** (PT/EN) and echo the resolved scope.

## When unsure

- **Ticker parsed but not held** (e.g. `PETR4` on an account that never held it) → the
  receipt may belong to a different account, or the position predates the book. Report; don't
  link to something not held.
- **Fuzzy name matches two holdings closely** (two share classes of the same manager) → the
  resolver returns `REPORT` for lack of a clear winner. Surface both candidates for the user.
- **A `PENDING` row's `Asset` is NULL** (not just `AssetRelated`) → it's not a clean income
  link; it's a stuck-PENDING completion (R1) — out of this skill's scope, report it.
- **The description names an asset the account once held but has fully sold** → it's still in
  the traded universe, so it can be a valid late income payment; link it, but note the asset
  is no longer a live position.
- **Layout C: an ISIN/CUSIP that hits no asset in the identifier index** → the security isn't
  registered in `Global.Asset` yet. This is an `asset-register` hand-off, not a link; report
  and stop.
- **Layout C: ISIN and CUSIP on the same row resolve to different assets** → data conflict in
  `Global.Asset` / `Portfolio.AssetCustody`. Do **not** pick one — surface both and let the
  user reconcile the master data first.
- **Layout C match on an asset the account has never touched** → do **not** treat this as a
  coherence failure. That's the whole reason C waives the holding gate. Still, note it in the
  report so a human can eyeball whether the coupon really belongs to this account (rare mis-
  routing by the custodian).
- **A whole account is lock-blocked but the user wants it linked** → explain the CheckedDate
  move, get explicit approval, emit the `CheckedDate_Update` `EXEC`, then re-run this skill.
