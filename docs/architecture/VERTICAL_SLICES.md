# Vertical slices

As of 17 October 2026. Status: working plan, owned by the backend.

## Why

Until now every component grew on its own: the hub collects and now publishes snapshots, core prices and risks, the prototypes show a mock desk, and the backend exists only as documents. None of the pieces has been checked against the others on real data.

From here, work goes in **vertical slices**. A slice is a thin path through every layer that the owner can open and check:

- it runs on **real hub data** (public market and static data);
- it uses **real core calculations**;
- it shows the result in a screen.

Each slice keeps whatever it built that matches the architecture. Where it takes a shortcut, the shortcut is named, and a later slice replaces it. ROADMAP milestones M1–M7 stay the destination; the slices are the order in which we reach them.

## Shortcuts that slice 1 takes on purpose

| Architecture target | Slice 1 shortcut | Replaced in |
|---|---|---|
| Backend reads snapshots from the hub API (`/v1/snapshots`) | Reads bundle files that the hub's own builder produced from a copy of the seed database (`tools/devdata/build_dev_snapshots.sh`) | S4, once the hub merges its snapshot layer |
| Desk Postgres store with run lineage (ADR-006) | The `tdm` SQLite store through its writer SDK (`tdm.writer.Run`); lineage goes into the run log and `lib_version` | S4 |
| Desk API v1 + React screens (05) | `tdm serve` and the existing Desk Monitor prototype through `window.DESK` | S4 (API), S5 (first React screen) |
| KBD/ZCYC evaluator in core (core package 0.6) | Backend module `desk.curves.kbd`, which builds a core `DiscountCurve` from the published ZCYC parameters | When core delivers 0.6 |
| Real positions (Q1 deferred) | A synthetic demo book over real instruments (`backend/examples/demo_book.csv`) plus a "universe" run: one bond of every priceable instrument | When Q1 is answered |

## Slices

| Slice | What the owner sees | Checks which pieces | Status |
|---|---|---|---|
| **S1 EOD fixed bonds on KBD** | Desk Monitor with a real 15.10.2026 run: PV, yield, Z-spread, DV01 by bucket, CS01, gamma for fixed-coupon OFZ and corporates of the demo book. A **validation report** per bond: our yield, duration, G-spread and accrued vs MOEX analytics from the same snapshot; coverage list with a reason for every bond not priced | Hub snapshot contract on the full universe; adapter; core 0.8.1 fixed-cashflow engine; KBD curve; writer SDK; prototype data path | **Done 17.10.2026** (see below) |
| **S2 Day-over-day flash PnL** | Second run on the next date with PnL by factor: carry, rates, spread, residual, using two snapshots and exact repricing | Snapshot history; core `reprice`; run-to-run linking | **Done 17.10.2026** (see below) |
| **S3 Floaters and the RUONIA curve** | OFZ-PK and key-rate floaters priced with fixings and projection; a RUONIA OIS curve from the hub's quote sets in the curves panel | Hub fixings and quote sets; core `FloatingLeg`, OIS bootstrap; curve-spec mapping `dataset` → spec | Next |
| **S4 Desk backend proper** | Same screens, now served by the desk API from the desk Postgres store, with run lineage; hub read over HTTP | ADR-002/006, 04, 05 §2 | |
| **S5 Interactive scenario** | Scenario slider with instant Taylor (≈) and an "exact" button that reprices on the backend; residual shown | 03 T1/T2, parity fixtures; first React + TypeScript screen | |
| **S6 Intraday** | Intraday run every N minutes on intraday cuts; badge with age | Hub intraday cuts; scheduler | |
| **S7 Hybrid book** | IRS, FX and other products imported from contract 1.3 files next to computed bonds, with coverage states | `private_inputs`, hybrid merge | |

Each slice ends with:
- a short note in this file: what works, what was found, what moved to the backlog of which repo;
- a test that runs the slice on the hub's golden fixtures in CI. Real-data runs stay local, because the snapshots are not committed.

## S1 detail

**Flow.**

1. Market snapshot of `D`, plus the static snapshot it names.
2. `desk.marketdata.bundle` reads the files and verifies the sha256.
3. `desk.marketdata.adapter` builds instruments: the known coupon schedule plus redemptions, the next put offer as horizon, and the selected quote with its basis.
4. `desk.curves.kbd` turns the ZCYC parameters into a `DiscountCurve`.
5. `desk.valuation.bonds` runs core `BondRiskEngine` (`continuous_zero` basis):
   - Z-spread calibrated to the dirty price;
   - bucket DV01 / KRD, CS01 or OFZ spread risk;
   - Hessian;
   - yield and duration through core `compatibility`.
6. `desk.publish.tdm_run` writes one `tdm` EOD run.
7. `desk.report.validation` writes the validation report.

**Pricing scope** (everything else is listed with a reason, never dropped):
- RUB, fixed coupon;
- every future coupon period `known`, with an amount;
- a redemption schedule summing to the current face;
- not perpetual, not structured, not face-indexed.

**Price basis** (provisional until a DEC decides it): MOEX legal close, then close, then weighted average. The basis used is stored on every position.

**Acceptance.**
- Every OFZ-PD and fixed corporate bond in scope is priced or rejected with a reason.
- For bullets priced to maturity, our yield at the MOEX price basis agrees with the MOEX `yield_close_pct` to within 1 bp. Larger differences are listed as findings for the hub or core backlog.
- Desk Monitor opens on the run.

## S1 result (17 October 2026)

How to run it: [`backend/README.md`](../../backend/README.md).

**What works.**
- Real `eod_final` snapshots for 09–15.10.2026 are rebuilt from the hub seed with the hub's own builder:
  - 4214 instruments, about 3440 bond quotes per day;
  - the KBD reproduces MOEX standard yields to 0.005 bp.
- One EOD date takes about 7 s:
  - 1034 bonds priced over the whole universe, with first-order risk, in about 3 s;
  - the 28-position demo book with the full Hessian;
  - the validation report.
- The Desk Monitor opens on the runs of 14 and 15 October: positions, bonds, DV01 by bucket, CS01, curve moves.
- Every bond not priced has a reason in the run log and the report.

**Comparison with MOEX, 15.10.2026.** Δy is the effective yield at the weighted-average price, measured from the trade date.

| Class | Priced | ok (≤ 1 bp) | warn (≤ 10 bp) | mismatch | Median abs Δy |
|---|---|---|---|---|---|
| OFZ-PD | 30 of 31 | 26 | 4 | 0 | 0.24 bp |
| Corporate | 949 of 2368 | 441 | 269 | 89 | 0.69 bp |
| Subfederal | 46 of 63 | 17 | 20 | 3 | 1.57 bp |

Most mismatches are bonds 1–3 months from their horizon. There a price difference of a few kopecks is many basis points of yield, while durations agree to 1–2 days.

**Findings and where they go.**

| # | Finding | Goes to |
|---|---|---|
| F1 | MOEX measures effective yield and duration from the **trade date**, not the T+1 settlement date. With T+1 the bias was +0.9 bp (OFZ) and +4 bp (corporates); with the trade date it is −0.2 / −0.1 bp | Done in the report. A DEC entry must fix which date the desk shows yields from |
| F2 | The yield horizon is the next offer **whose exercise window is still open**, including issuer calls; MOEX skips puts whose window has closed | Done in `valuation/bonds.py`. Core's future `Instrument` (package 0.1) needs `window_end` and the call/put type |
| F3 | 314 instruments have a quote without accrued interest, and 130 have no price (no trades). Every instrument carries `day_count_not_published`, so neither the backend nor core can compute accrued itself | Hub: publish MOEX accrued for every instrument (securities board data), not only for traded ones. Core: accrued from the schedule needs the day count |
| F4 | Some `known` coupon amounts contradict MOEX accrued. Example: RU000A101M04, next coupon 30.21 RUB against accrued 36.45 RUB | Hub: bond static verification (hub STATUS, remaining step 3) |
| F5 | For bonds close to their horizon, comparing yields is not meaningful | Backend: compare in price space next |
| F6 | The dev export builds every historical cut with today's `known_at` | Accepted for development. Real runs read published snapshots (S4) |
| F7 | The hub's snapshot layer sits in a worktree based on an older main (migrations 0423/0424 missing there) | Hub: merge the snapshot layer |
| F8 | Distressed names dominate "most traded" corporates (yields of 200–400 %) | Demo book filters them out; the real book will contain whatever the desk holds |

The S1 comparison table above was measured with the T+1 valuation date. S2 moved the valuation to the trade date (finding F9 below). On 15.10 the corporate row is now 452 ok, 272 warn, 75 mismatch, with a median of 0.65 bp. The subfederal row is 19 ok, 20 warn, 1 mismatch, median 1.18 bp. The OFZ-PD row is unchanged.

## S2 result (17 October 2026)

**How to run it.** `desk eod` now attaches the flash PnL whenever the snapshot folder holds an earlier `eod_final` date; `--no-pnl` turns it off. Run the dates in order to get the history:

```bash
cd backend && for d in 2026-10-12 2026-10-13 2026-10-14 2026-10-15; do .venv/bin/desk eod --snapshots ../.devdata/snapshots --date $d --book examples/demo_book.csv --db ../.devdata/tdm.sqlite; done
```

Each date takes about 2 s; the previous date is revalued on its own snapshot. The book is the same on both dates, so there are no trades of the day.

**Method** (`backend/src/desk/pnl/flash.py`, exact repricing with core 0.8.1, no Taylor). The D-1 position (its cashflows, quantity and calibrated Z-spread `z0`) is repriced step by step through core `BondRiskEngine` with `spread_bp` as input. Each step changes one thing:

| Step | Curve | Date | Spread | Component (difference to the step above) |
|---|---|---|---|---|
| V0 | KBD of D-1 | D-1 | `z0` | equals the D-1 dirty value |
| V_carry | KBD of D-1 | **D** | `z0` | `carry`: time on unchanged forwards and spread; coupons and redemptions paid in (D-1, D] are added here |
| V_roll | ZCYC parameters of D-1, dated D | D | `z0` | `rolldown`: "same yield per tenor" vs "forwards realized" |
| V_rates | **KBD of D** | D | `z0` | `rates` |
| V_spread | KBD of D | D | **`z1`** (calibrated on D) | `spread` |
| V1 | | | | `other`: the D dirty value minus V_spread. It is not zero when the D-1 cashflows differ from the D schedule, for example after an offer passed |

- `rates` and `spread` are full repricings, so second-order effects are inside them. `convexity` is 0 on purpose.
- Check per position: `carry + rolldown + rates + spread + other = q·(dirty_D − dirty_D-1) + received`, where both dirty prices come from the published valuations. If the error is more than 0.01 RUB, the position gets no PnL and a WARN `identity_failed`.
- A position not priced on either date gets no `pnl` and a WARN with the reason, for example `not_priced_prev: 2026-10-13: no_quote`. A stale quote (the same trade date on both days) or a changed quantity is treated the same way.
- Each position's `extra.pnl` keeps the diagnostics: `z0`, `z1`, cashflows received, total, identity error, and `rates_linear` (Σ D-1 bucket DV01 × zero-rate move per pillar).

**What works.**
- The Desk Monitor PnL tab shows real flash PnL for 12–15.10 instead of "positions without PnL". It has the waterfall by component, books, and the 22-day history with explained PnL. Official PnL stays "expected T+1".
- The identity holds on every position and every date: the largest error is 3·10⁻⁴ RUB on positions of up to 2 bn RUB.
- On 15.10 the book made +34.78 m RUB: carry +4.80, rolldown +1.68, rates +24.36, spread +3.94, other 0.

Demo book, m RUB:

| Date | Book | Positions | Carry | Rolldown | Rates | Spread | Other | Total | Received | Σ\|rates − linear\| / Σ\|rates\| |
|---|---|---|---|---|---|---|---|---|---|---|
| 12.10 (3 days) | OFZ | 12 | 9.55 | 4.38 | 77.25 | −23.12 | 0 | 68.05 | 0 | 1.9 % |
| | CORP | 15 | 4.15 | 1.28 | 1.72 | 4.15 | 0 | 11.31 | 6.57 | 16 % |
| 13.10 | OFZ | 12 | 3.13 | 1.50 | 25.47 | −9.83 | 0 | 20.27 | 0 | 2.3 % |
| | CORP | 15 | 1.34 | 0.45 | 1.07 | −2.03 | 0 | 0.83 | 0 | 20 % |
| 14.10 | OFZ | 12 | 3.25 | 1.38 | 26.89 | 3.76 | 0 | 35.28 | 66.76 | 0.9 % |
| | CORP | 15 | 1.38 | 0.41 | 1.43 | −5.99 | 0 | −2.77 | 3.60 | 16 % |
| 15.10 | OFZ | 12 | 3.32 | 1.28 | 19.78 | 8.07 | 0 | 32.46 | 0 | 1.3 % |
| | CORP | 16 | 1.47 | 0.40 | 4.58 | −4.13 | 0 | 2.32 | 3.12 | 8 % |

CRP-05 has no PnL on 14.10: it had no quote on 13.10.

**Findings and where they go.**

| # | Finding | Goes to |
|---|---|---|
| F9 | MOEX accrued is measured to the **trade date**. Over 09–15.10, 6661 of 7675 quotes in a coupon period match the accrued to the trade date and none match T+1; the other 1014 match neither (different day counts). The bond trades cum-coupon through the record date. S1 discounted from T+1 and dropped cashflows on or before T+1. On a record date it therefore lost the coupon paid the next day while the price still contained its accrued. Example: ODK RU000A10FG76 on 15.10, Z 266 → 194 bp, and the PnL counted the 4.07 m RUB coupon twice. The valuation date is now the trade date for every bond; T+1 is kept as `settlement` for information. The S1 comparison improved (see above) | Done in `valuation/bonds.py`. DEC entry: valuation and yield date = trade date (closes F1) |
| F10 | OFZ spreads to the KBD move by several bp a day (SU26228: −51 → −43 bp on 15.10). MOEX's published Z-spreads move in the same direction (SU26254: 39 → 36 bp). This puts −23 m to +8 m RUB a day of the OFZ book into `spread`. The KBD is a smooth fit, so part of the daily OFZ move is the fit's residual, not the curve | Desk: decide whether OFZ show `spread` vs KBD or a curve fitted to the desk's own OFZ. Hub: nothing |
| F11 | The KBD short end is volatile: on 15.10 1M moved +36 bp and 3M +27 bp, while 2–5Y moved −8 to −11 bp. For short corporates, pillar DV01 × pillar move misses 8–20 % of the exact `rates`, because the curve's move between pillars is not linear in the pillar moves. For OFZ it misses 1–2 % | Expected. S5 (Taylor vs exact) should show this residual. The "Rates explain" matrix of the Desk Monitor will not match `rates` for short bonds |
| F12 | The legal close and the close can differ by 0.5 % on a liquid bond (Polyplast 15.10: 98.30 vs 98.83). The day's PnL then depends on the price basis: with 250 000 bonds that is 1.3 m RUB | DEC on the price basis (open since S1) |
| F13 | `other` was 0 on every position: no schedule changed between two dates of the demo book. A position whose offer passes, or with a newly fixed coupon, will show it here | Check again on the real book |
| F14 | Rolldown on the KBD is 30–40 % of carry on these dates | Information |

