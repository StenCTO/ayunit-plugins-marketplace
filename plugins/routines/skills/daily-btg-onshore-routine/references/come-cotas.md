# Recipe: Come-cotas SELL

Twice-yearly (May and November) BR fund income tax withholding — the tax is
paid **by surrendering quotas**, not by taking cash. The loader lands these
as `SELL` with `Status='PENDING'` because they violate its standard
sign/price assumptions (`Value = 0` but `ValueGross > 0`, `Quantity < 0` on a
`Value = 0` trade). Manual promotion via a known recipe.

Recipe originally documented in the project's root `CLAUDE.md §6`. Mirrored
here so the orchestrator's reference library is self-contained.

## Detection

A `PENDING` row is come-cotas when **all** of:

- `TransactionType = 'SELL'`
- `GeneralLedgerDescription LIKE '%COME COTAS%'` (BTG's spelling; may include
  `RESGATE IR REF. APLIC. …/… (COME COTAS)`)
- `RawTransaction.launchType = 'RI'` (Redemption-IR)
- `Value = 0` (no cash movement — quotas surrendered)
- `ValueGross > 0` (the IR amount that would have been paid in cash)
- `PriceExFee > 0` (the fund NAV on the come-cotas date)
- `Quantity < 0` (quotas surrendered)
- `Asset IS NOT NULL` and `Global.v_Asset.Activated = TRUE` (fund exists,
  active — not the deactivated-fund case)
- Identity holds: `ROUND(ValueGross - ABS(Quantity) × PriceExFee, 2) ≈ 0`

The identity is the strongest signal: if it doesn't hold to within R$0.02,
this is **not** come-cotas — investigate as an ordinary SELL with wrong
Value.

## Fix recipe

Standard SELECT-first-merge `@CMD='U'` — keep every field, only overlay
`Status` and `AgentCheck`:

1. Fresh SELECT of every populated column.
2. Drop `AccountCurrency` / `AccountFx`.
3. Preserve `RawTransaction`.
4. Absolute values for `Quantity` / `Price` / `PriceExFee` / `Value` / `ValueGross`.
5. **Overlay:**
   - `Status = 'UPDATED'` — `UPDATED` counts like `VALIDATED` in the pipeline
     and skips the strict "Price required" check (`Value = 0` and `Price = 0`
     would otherwise fail), see [`ayunit://docs/portfolio-creator/pipeline`](ayunit://docs/portfolio-creator/pipeline) §2.
6. Keep everything else as the loader stored it:
   - `Price = 0` (no unit cash price)
   - `Value = 0` (no cash movement)
   - `ValueGross = the IR amount` (the tax being paid)
   - `Quantity = -(quotas surrendered)` (proc applies the sign — pass absolute)
   - `PriceExFee = the fund NAV on the come-cotas date`
7. `AgentCheck`:
   ```
   fix <YYYY-MM-DD>: come-cotas SELL on <Asset> - PriceExFee=NAV <NAV>,
   ValueGross=IR amount <IR>, Price=0, Value=0 (no cash), Quantity=quotas
   surrendered; Status PENDING->UPDATED [CC]
   ```
8. `execute_procedure(procedure='Portfolio.AccountTransaction_Update', cmd='U', params={…})`.

## AgentCheck tag

`[CC]` — come-cotas.

## Why the pipeline handles this correctly

Once promoted, the pipeline consumes:

- `Quantity` (negative) → reduces `QuantityClose` on the asset side. ✅
- `Value = 0` → no cash-side write (Step 2.5 evaluates to no-op). ✅
- `ValueGross > 0` → feeds `SellIncomeTaxes = Value − ValueGross < 0` (the
  IR withholding). ✅

The come-cotas event thus (a) reduces the fund quota count without a
matching BRL debit, and (b) records the IR as a negative `SellIncomeTaxes`
attribute — exactly how BTG's real-world custody snapshot lands the
reduction on `SettlementDate` (typically T+3, i.e. `Date=2026-05-29,
SettlementDate=2026-06-02`).

## What this is NOT

- **Not for `DEPÓSITO DE COTAS E TRIBUTOS PROVISIONADOS`.** That's a
  structurally similar but distinct tax-provisioning artifact — see
  [`deposito-tributos-provisionados.md`](deposito-tributos-provisionados.md).
  Come-cotas has description containing `COME COTAS`; the DEPÓSITO variant
  does not.
- **Not for a deactivated fund.** If `Global.v_Asset.Activated = FALSE`, the
  event is residue, not real come-cotas — see
  [`deactivated-fund-residue.md`](deactivated-fund-residue.md).
- **Not for other custodies.** Come-cotas is BR-onshore-specific. Offshore
  funds have different tax mechanics; do not apply this recipe outside BTG
  onshore.

## Real-world examples (verified 2026-07-15)

Account `004751948`, BTG onshore. 4 rows on 2026-05-29 with `SettlementDate=2026-06-02`:

| pk | Asset | Qty (raw) | PriceExFee (NAV) | ValueGross (IR) | Description |
|---|---|---:|---:|---:|---|
| 75385 | Sten Master D5 | −2,377.44 | 1.31965 | R$3,137.40 | RESGATE IR REF. APLIC. 27/02/2026 (COME COTAS) |
| 75386 | Sten Master D5 | −725.38 | 1.31965 | R$957.25 | RESGATE IR REF. APLIC. 08/01/2026 (COME COTAS) |
| 75387 | Sten Master D5 | −1,762.90 | 1.31965 | R$2,326.42 | RESGATE IR REF. APLIC. 07/01/2026 (COME COTAS) |
| 75388 | Sten Master D30 | −564.51 | 1.34711 | R$760.46 | RESGATE IR REF. APLIC. 27/02/2026 (COME COTAS) |

Sum of D5 pending quotas: 4,865.72. Custody's Sten Master D5 quantity dropped by exactly 4,865.72 on the SettlementDate 2026-06-02. Recipe applied — position matches custody to 6 decimals after recompute.

Identity checks all hold:
- 2,377.44 × 1.31965 ≈ 3,137.40 ✓
- 725.38 × 1.31965 ≈ 957.25 ✓
- 1,762.90 × 1.31965 ≈ 2,326.42 ✓
- 564.51 × 1.34711 ≈ 760.46 ✓

All four promoted with `[CC]` tag.
