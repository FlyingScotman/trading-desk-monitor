# Brief for the calculation core

As of 18 October 2026 (S4 items added 19 October), against core **0.32.0** (commit `c0af388`); status of 22 October below. From the desk system (this repository) to the work on `rub-fixed-income-core`. The core's own `docs/PRODUCT_ROADMAP.md` and `docs/BACKLOG.md` remain the place where core tracks its plan; copy the items you accept there.

The items come from:
- the backend's slices S1–S2 on real snapshots ([VERTICAL_SLICES](../VERTICAL_SLICES.md), findings F1–F14);
- the backend's move from core 0.8.1 to 0.32.0.

## Status (22 October 2026)

Checked against core's `CHANGELOG.md` and `docs/BACKLOG.md` (branch `claude/blissful-ramanujan-ukia51`). Items marked **done** in the table below landed in core **0.33.0**: C1 (`Offer.window_start` / `window_end`, `horizon="open_put"`), C4 (`docs/SYSTEM_CONTEXT.md` refreshed against ADR-001), C7 (fixed bonds whose coupons are not known to the horizon are rejected or flagged), C8 (spread kind per instrument, `ValuationPolicy.spread_kind_for`). The backend pins 0.33.0 (commit `5ec23b3`).

**Picked up:** core **0.36.0** (commit `24fe776`; the work that was numbered 0.34.0 / 0.35.0 on core's branch, `15f63e3` / `f3cbabc`, plus core's 0.34.0 and never-called perpetuals): CNY / USD / EUR bonds on their own currency's curve (core BACKLOG #132) and the FX book are in the backend. **Awaiting the desk:** the KBD as a risk curve and KBD floaters (core 0.34.0, option C; core BACKLOG #124: OFZ on `rub_ofz_kbd_v2`, KBD floaters off the fallback, `basis_view`) and never-called perpetuals (core BACKLOG #128: `ExerciseAssumption.never_called()` instead of `perpetual_call`); not started.

## The backend on 0.32.0

- `backend/pyproject.toml` pins core by commit from its GitHub repository: tags stop at `v0.3.0`.
- Tests, the validation report (4033 instruments) and the flash PnL are identical to 0.8.1. The only change was one deprecated call ([VERTICAL_SLICES](../VERTICAL_SLICES.md), "Core 0.32.0 recheck").
- Since slice S4 (19 October) fixed-coupon bonds are valued by `value_portfolio` over `Instrument`/`MarketSnapshot`/`Position` on core's KBD (`rub_ofz_kbd_v1`). PV and the flash PnL are unchanged against S1/S2; the old backend KBD curve was off by up to 6 bp at the short end, core's by at most 0.003 bp. Instruments are built with `settlement_lag_days=0` (C2): Z then matches the trade-date valuation to 0.003 bp, against 0.5–2.8 bp off with the default lag.

## Items

| ID | Item | Why (evidence) | Check |
|---|---|---|---|
| C1 **done** | **Offer exercise windows.** `Offer` has `offer_date`, `kind` and `confirmed`, but no exercise window. `HorizonPolicy` knows `maturity` and `confirmed_put` only, and never assumes a call | F2: MOEX's yield horizon is the next offer **whose exercise window is still open**, including issuer calls. Puts whose window has closed are skipped. With the first confirmed put after settlement, a bond whose put window closed yesterday is valued to the wrong date. The backend applies the open-window rule itself today (`valuation/bonds.py`) | An `Offer` field for the end of the exercise window (and its start, if known), and a horizon policy "next offer with an open window" (calls included per issuer terms). Golden: a bond between the end of its put window and the put date, against MOEX `YIELDTOOFFER` |
| C2 | **Valuation at the trade date.** `value_portfolio` values at `settlement = business_date + settlement_lag_days` (default 1) | F9: MOEX prices and accrued are measured to the **trade date**, and a bond trades cum-coupon through its record date. Valuing at T+1 drops a coupon paid on T+1 while the price still contains its accrued. Example: RU000A10FG76 on 15.10.2026: the Z-spread falls from 266 to 194 bp, and the day's PnL counts the 4.07 m RUB coupon twice. The backend values at the trade date and keeps T+1 only as information | Either a valuation-date choice in `ValuationPolicy`, or a documented rule that the desk sets `settlement_lag_days=0`, with the record-date case tested. Golden: RU000A10FG76 on 15.10.2026 (in the hub's EOD data) |
| C3 | **Release tags.** Tag every release from 0.4.0 on, at least from 0.32.0 on. `docs/CONSUMERS.md` says the desk backend "pins a tagged release"; no such tag exists | The backend must pin a commit instead. The Windows wheelhouse ([06](../06_DEPLOYMENT_AND_OPERATIONS.md) §7) needs a wheel built from a named release | `git tag` lists `v0.32.0`; the backend switches its pin to the tag |
| C4 **done** | **`docs/SYSTEM_CONTEXT.md`** still says "written at 0.8.1" and describes the 16 October hosting | Since 18 October one hub on a VPS serves every consumer (ADR-001 amended); the backend pulls snapshots into its own cache. The Windows / Python 3.11 runtime target is unchanged | Refreshed against ADR-001 and this brief |
| C5 | **Horizon explain vs the backend's flash PnL.** The backend explains day-over-day PnL by stepwise exact repricing (V0 → carry → rolldown → rates → spread → other), with an identity check per position ([VERTICAL_SLICES](../VERTICAL_SLICES.md), S2 method). Core 0.14 has `explain_horizon_pnl` for the same split | S4 runs both on the same positions. Points to align: <br>• rolldown as "ZCYC parameters of D−1, dated D" in the backend vs "constant curve shape" in core; <br>• coupons and redemptions paid in (D−1, D]; <br>• an `other` component when the schedule changed between the two dates (an offer passed, a coupon newly fixed: F13) | Per-component differences on the dev snapshots of 09–15.10 below 1 RUB per position, or each difference explained |
| C6 | **Corporate accrual basis in the field.** Core 0.17 reproduces MOEX accrued with `RATE_ACT_365` on 18 golden bonds | F9: 1014 of 7675 quotes in coupon periods match neither accrued to the trade date nor to T+1 under a plain day count. S4 rechecks them with core's rate basis | After S4, the backend lists any remaining mismatches with ISINs; core decides whether they are conventions (BACKLOG) or data errors (hub brief H8) |
| C7 **done** | **Unknown coupons on the fixed path.** `Instrument.cashflows` uses whatever periods it is given; nothing checks that coupons are known up to the horizon (the callable path does, `_check_coupons`) | RU000A0JXQ44 has unknown coupons after 2028-04-13: valued to maturity without them, it is under-priced with no error. The backend checks this itself before building the instrument | `value_portfolio` rejects (or flags, with an assumption entry like `ExerciseAssumption`'s `assumed_coupons`) a fixed bond whose coupons are not known up to the horizon |
| C8 **done** | **Spread kind per position.** `ValuationPolicy.spread_kind` is one value per call | A mixed book (OFZ and corporates) needs one `value_portfolio` call per spread kind, so one run yields two results and two hashes | `spread_kind` per instrument or position (or a policy map), so one call values the whole book |

## Inputs core needs from the hub

These are hub work, listed in [HUB.md](HUB.md) H10–H15. Core needs no change for them:
- CPI series for OFZ-IN;
- futures settlement prices;
- the RUONIA index and declared fixing gaps;
- FX spots with value dates;
- foreign overnight fixings;
- CDS quotes (no source).

## Not core work

- Desk conventions: funding, OCP, dual FX basis, official PnL ([04](../04_BACKEND.md) §4).
- The open-window rule and trade-date valuation stay in the backend adapter until C1 and C2 land. Then they move to core.
