# Recipe: `AQUISICAO VIRTUAL` — fund-incorporation phantom BUY

BTG occasionally consolidates or migrates funds (e.g. STEN MFO family
absorbed into Sten Master family in May/June 2026: STEN MFO D5 → Sten
Master D5, STEN MFO D30 → Sten Master D30, STEN MFO DEB INFRA → STEN DEB
FIF INFRA). The **correct** economic representation is an ASSET DELIVERY
of the old fund + an ASSET RECEIPT of the new fund on the incorporation
date, no cash movement (verified 2026-08-24 on account 004434131,
2026-05-11: pks 103806/103808 ASSET DELIVERY STEN MFO D30 + pks
103807/103809 ASSET RECEIPT Sten Master D30 — booked cleanly).

Alongside those correct rows, BTG also emits **phantom shadow BUY
entries** with description `AQUISICAO VIRTUAL` on the following day. These
carry `Value < 0` (implying cash out) but do **not** correspond to any
real cash movement in custody. Promoting them via `pending-revalidate`
would:

- **Duplicate the incorporation** when the ASSET DELIVERY/RECEIPT pair
  already booked correctly (double-counts the received-side quantity), or
- **Land a phantom BUY** on a fund that was already fully redeemed before
  the trade date (verified 2026-08-24 on account 004434131 STEN MFO D5:
  redeemed in full 2026-04-20 via RESGATE TOTAL pks 48675/48676, then 3
  spurious AQUISICAO VIRTUAL BUYs on 2026-05-12 for R$208k each = R$624k
  of phantom cash-out that has no custody counterpart).

The correct disposition is `Status = 'IGNORED'` — the row stays for audit
but never enters `AccountPosition`.

## ⚠️ AQUISICAO VIRTUAL is NOT always a phantom, AND IGNORE has a cash-side blindspot

BTG uses the label `AQUISICAO VIRTUAL` for at least THREE distinct
patterns:

1. **True phantom / shadow of an incorporation pair** — signal A fires,
   IGNORE is safe (position AND cash both correct because the real
   incorporation pair already captured both sides). Verified 2026-08-24
   on account 004434131 pk 58662 (STEN MFO D30, matched by pks
   103806-103809 on 2026-05-11).
2. **Real virtual-book position acquisition** — the row corresponds to a
   real custody quantity jump on trade date +1/+2. **Do NOT IGNORE**;
   promote via `pending-revalidate`. Verified 2026-08-24 on account
   004434131 pk 100207 (Sten SSS BUY 20,525.73 units on 2026-07-13 →
   custody qty jumped 261,595.68 → 282,189.46 on 2026-07-14).
3. **Cash-consuming in-transit residue on a redeemed / never-held fund**
   — the row's cash IS spent (custody BRL drops by `|Value|`) but the
   resulting fund position appears in custody as `Qty=0 Value=|Value|`
   for ~10 business days then disappears. Signal B fires (fund not
   held), so this recipe currently IGNOREs. **The position side is
   correct** (target-date custody has 0 qty, so book shouldn't hold it
   either), **but the cash side is NOT** — book cash stays high because
   nothing consumed the R$|Value| that custody debited. Verified
   2026-08-24 on account 004434131 pk 59547 (STEN MFO D5 R$208,000 on
   2026-05-12): custody BRL delta on 05-12 was R$203,683 short of book's
   R$411,683 delta (exactly R$208,000 gap), traced to a STEN MFO D5
   custody in-transit row `Qty=0 Value=R$208,000` at Price=1.22631198
   (exact match to pk 59547's Price) living 05-13 → 05-25 then vanishing.

**Recipe limitation.** Signal B alone cannot distinguish pattern 3 from
pattern 1. Post-IGNORE, always run a same-day cash-side sanity check:

```sql
-- Sum |Value| of AQUISICAO VIRTUAL rows IGNOREd on <trade_date>
-- versus custody BRL delta on <trade_date> vs prior trading day.
-- Any unexplained gap ≈ N × |Value| of an IGNORE means at least N of the
-- IGNOREs were pattern 3 (real cash-consuming) and the account needs a
-- human decision on how to represent the cash on the book side
-- (typical options: (a) write a synthetic WITHDRAW BRL <amount>
-- reflecting BTG's real cash debit; (b) promote the AQUISICAO VIRTUAL
-- as UPDATED and accept a short-lived phantom asset position).
```

**Autonomy stance:** the routine still runs the IGNORE (position side is
right), but the cash-side residual is a mandatory human-action item.
Never write a synthetic WITHDRAW autonomously from this recipe — the
compensating amount depends on how many IGNORE rows fall in pattern 3
vs pattern 1, which currently cannot be distinguished from AccountTransaction
data alone (requires reading `v_CustodyPosition` on the trade date for
Qty=0/Value>0 in-transit rows).

## Detection signals (all must hold)

| Signal | Test |
|---|---|
| Row is `PENDING` | `Status = 'PENDING'` |
| Loader-classified as BUY | `TransactionType = 'BUY'` |
| Description marker | `GeneralLedgerDescription LIKE '%AQUISICAO VIRTUAL%'` (case-insensitive; BTG's spelling — no cedilla) |
| Asset resolved | `Asset IS NOT NULL` (loader could resolve the CNPJ/AnbimaCode) |
| `Value < 0`, `Quantity > 0` before proc sign convention (loader landed BUY-shape) | `Value < 0 AND Quantity > 0` in the raw row |
| **One of the two disqualifying-existence checks fires** (at least one — you don't need both) | See A and B below |

### A. Incorporation pair already booked

There exists a matched pair on this `(Account, Custody)` within a **±7-day
window** of the AQUISICAO VIRTUAL row:

- an **ASSET DELIVERY** row on the resolved `Asset` (the old fund) — any
  status `IN ('VALIDATED', 'UPDATED')`,
- **and** an **ASSET RECEIPT** row on some other Asset (the new fund) —
  same status filter,
- with `ABS(ValueGross)` on the ASSET DELIVERY ≈ `ABS(ValueGross)` on the
  ASSET RECEIPT (`|Δ| < 0.5% × ValueGross` tolerance).

The AQUISICAO VIRTUAL row is a shadow of that already-handled event.

### B. Fund already fully redeemed / never held again

The resolved `Asset`'s calculated position is **zero** on the day before
the trade date **and stays at zero** through the account's latest
`AccountPosition` snapshot:

```sql
WITH t AS (
    SELECT ClientAccount, Custody, Date, Asset
    FROM Portfolio.v_AccountTransaction WHERE pk_AccountTransactionID = <pk>
),
prior AS (
    SELECT ABS(COALESCE(QuantityClose, 0)) AS prior_qty
    FROM Portfolio.v_AccountPosition p JOIN t
      ON p.Account = t.ClientAccount AND p.Custody = t.Custody AND p.Asset = t.Asset
    WHERE CAST(p.[Date] AS date) = DATEADD(day, -1, t.Date)
),
latest AS (
    SELECT ABS(COALESCE(QuantityClose, 0)) AS latest_qty
    FROM Portfolio.v_AccountPosition p JOIN t
      ON p.Account = t.ClientAccount AND p.Custody = t.Custody AND p.Asset = t.Asset
    WHERE CAST(p.[Date] AS date) = (
        SELECT MAX(CAST([Date] AS date)) FROM Portfolio.v_AccountPosition
        WHERE Account = t.ClientAccount AND Custody = t.Custody AND Asset = t.Asset
    )
)
SELECT prior.prior_qty, latest.latest_qty,
       CASE WHEN COALESCE(prior.prior_qty,0) < 1e-3
             AND COALESCE(latest.latest_qty,0) < 1e-3
            THEN 1 ELSE 0 END AS confirms_no_holding
FROM prior FULL OUTER JOIN latest ON 1=1;
```

`confirms_no_holding = 1` → signal B fires.

## Fix recipe — `Status = 'IGNORED'`

Standard SELECT-first-merge:

1. Fresh SELECT of every populated column.
2. Drop `AccountCurrency` / `AccountFx`.
3. Preserve `RawTransaction`.
4. Absolute values for `Quantity` / `Price` / `PriceExFee` / `Value` / `ValueGross`.
5. **Overlay only:**
   - `Status = 'IGNORED'`
   - `AgentCheck` (fill in whichever signal fired):
     ```
     fix <YYYY-MM-DD>: AQUISICAO VIRTUAL phantom BUY on <Asset> -
     <signal A: matched by ASSET DELIVERY/RECEIPT pair pks <D>/<R> on <pair_date>
      |
      signal B: fund fully redeemed pre-trade (prior_qty=<q1>, latest_qty=<q2>),
                no subsequent holding>;
     BTG shadow entry, no real cash movement.
     Status PENDING->IGNORED to prevent phantom BUY in AccountPosition [FUND-INC-DUP]
     ```
6. `execute_procedure(procedure='Portfolio.AccountTransaction_Update', cmd='U', params={…})`.

## AgentCheck tag

`[FUND-INC-DUP]` — distinct from `[PR-IGN-DEACT]` (deactivated fund),
`[PR-IGN-TAXPROV]` (tax provisioning), `[CC]` (come-cotas), `[DUP-REVERT]`
(loader duplicate), `[TROCA-NOME]` (FII name swap).

## What this is NOT

- **Not for `AQUISICAO VIRTUAL` on an active, currently-held fund with no
  incorporation pair.** If the resolved Asset is currently held in custody
  (`v_CustodyPosition` shows > 0 qty at latest snapshot) AND no matching
  ASSET DELIVERY/RECEIPT pair exists, the AQUISICAO VIRTUAL might be a
  real (if oddly-labeled) purchase. Escalate — do NOT auto-IGNORE.
- **Not for deactivated funds.** `deactivated-fund-residue.md` runs first
  in the pre-classifier and claims those. This recipe applies to active
  funds where the phantom BUY is a shadow of a real incorporation or a
  post-redemption artefact.
- **Not for `TROCA DE NOME` clusters.** Those carry `launchType='TROCA DE
  NOME'` in RawTransaction and are BUY/SELL misclassifications of a
  peg-priced swap. See `troca-de-nome.md`. AQUISICAO VIRTUAL has
  `launchType` `'TA'` (Transferência-Aplicação) or similar and does not
  come in a matched BUY/SELL cluster on the trade date.

## Real-world example (verified 2026-08-24)

Account `004434131`, BTG onshore. 3 PENDING AQUISICAO VIRTUAL BUYs on
2026-05-12:

| pk | Asset (resolved) | Qty | Price | Value | Signal |
|---|---|---:|---:|---:|---|
| 58659 | STEN MFO D5  | 169,666.82 | 1.22593212 | −R$208,000 | **B** — STEN MFO D5 fully redeemed 2026-04-20 (pks 48675/48676 RESGATE TOTAL), qty went to 0 and stayed there |
| 58662 | STEN MFO D30 | 166,650.06 | 1.24812435 | −R$208,000 | **A** — STEN MFO D30 handled 2026-05-11 by ASSET DELIVERY pks 103806/103808 (R$563,944) + ASSET RECEIPT Sten Master D30 pks 103807/103809 (R$563,944). AQUISICAO VIRTUAL BUY is shadow of that. |
| 59547 | STEN MFO D5  | 169,614.26 | 1.22631198 | −R$208,000 | **B** — same as pk 58659 |

Recipe applies to all three. Fix: `Status = 'IGNORED'` with the
`[FUND-INC-DUP]` tag. Without this fix, promoting them would drop
R$624k of phantom BRL cash-out into the book — inverting the sign of
the account's already-existing +R$252k BRL divergence.
