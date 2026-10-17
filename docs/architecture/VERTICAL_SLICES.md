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
| **S2 Day-over-day flash PnL** | Second run on the next date with PnL by factor: carry, rates, spread, residual, using two snapshots and exact repricing | Snapshot history; core `reprice`; run-to-run linking | Next |
| **S3 Floaters and the RUONIA curve** | OFZ-PK and key-rate floaters priced with fixings and projection; a RUONIA OIS curve from the hub's quote sets in the curves panel | Hub fixings and quote sets; core `FloatingLeg`, OIS bootstrap; curve-spec mapping `dataset` → spec | |
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
