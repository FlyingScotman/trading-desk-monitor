# 03 Calculation split between backend and frontend

This document answers the owner's question: **when a user changes stress-scenario parameters, does the backend recompute, or does the backend publish snapshot risk (first- and second-order) and let the frontend do the rest?**

**Answer: both, with clear rules.** The backend computes everything that becomes a record, and it publishes greeks rich enough for the browser to give an *instant approximation* while the user edits. When the user wants the real number, the backend revalues exactly in a few seconds.

Decision: [ADR-005](adr/ADR-005-calculation-split.md). This refines the three levels in `docs/SCENARIOS.md`.

## 1 Why not "all in the browser" or "all in the backend"

| Option | Problem |
|---|---|
| Browser does everything from published greeks | A Taylor expansion is only an approximation. `SCENARIOS.md` measures a 4% MV error at a 1,300 bp shock (the 2022 case), worse than first order. It is unreliable for floaters with caps and floors, offers, CDS jump-to-default, and products without gamma. Business rules (funding, OCP, official PnL) would also be duplicated in JavaScript; the mockups already show them drifting apart. |
| Backend does everything, synchronously | Dragging a curve pillar emits about 30 events per second. An exact revaluation for each one is wasteful and feels laggy even when each call takes about 1 s. |
| **Hybrid (chosen)** | Instant feedback from greeks, exact numbers on demand, one source of truth for anything persisted. |

## 2 The three tiers

| Tier | Runs in | Trigger | Computes | Latency budget (≤2k positions, **provisional**) | Persisted |
|---|---|---|---|---|---|
| **T3 batch, EOD** | Backend orchestrator | EOD cut | PV; first-order greeks (KRD by curve × bucket, CS01 / OFZ-spread01, FX delta); **second-order Hessian**; JTD; exact preset and historical scenarios; carry/funding projections for preset horizons; flash PnL; PnL explain from M5 | ≤ 15 min | Yes, as part of the run |
| **T3 batch, intraday** | Backend orchestrator | Every N minutes in session | PV, first-order greeks, flash PnL against the previous EOD. Hessian, JTD and scenarios are reused from the EOD run and labelled with their age. Intraday explain from M5 | ≤ 60 s | Yes, as an intraday run |
| **T1 instant** | Browser | Every edit (slider, drag, field) | Second-order Taylor PnL from published greeks, aggregated over the current filter | ≤ 50 ms per edit | **Never** |
| **T2 exact** | Backend job worker | "Compute exactly" button, or automatically after about 1 s of no edits | Full revaluation of the base run under the edited scenario, with per-position results and the residual against T1 | ≤ 5 s p95 (needs shared shocked-curve builds, §8) | Yes, as a scenario attachment to the base run |

These budgets are planning targets. They replace the provisional targets in `docs/program/EXECUTION_SPEC.md` §11, and become commitments only after the benchmarks in §8.

Carry and funding horizon scenarios with user-edited parameters (key-rate path, horizon, funding spread) run as a **synchronous backend call** (≤ 1 s). Projection is cheap (positions × days), but it carries desk conventions that must not exist twice ([04](04_BACKEND.md) §4).

## 3 Rules

1. **Only backend numbers are records.** Anything saved, shared, exported, reported, attributed or reconciled comes from T3 or T2. T1 numbers are labelled ≈ and never sent back for storage.
2. **The browser only filters and sums.** Per-position fields are published by the backend, including PnL components, official PnL, breaks, OCP contribution, funding, carry and coverage state. The browser aggregates them through **one shared selectors module**. It never derives official PnL, break status, OCP, funding or carry.
3. **One Taylor engine, two runtimes.**
   - The Python implementation (backend) is the reference.
   - The browser has a port of it.
   - Parity is enforced by fixtures generated in Python: per-position greeks + scenario → expected Taylor PnL by component, including cross terms (IR×CS, IR×FX, CS×FX).
   - The JS test suite fails on any mismatch above 1e-9 relative.
4. **Coverage is visible.**
   - A T2 or stored result is labelled "full revaluation" only when every position in view is covered.
   - Otherwise it shows `partial (n/N)` and lists what is missing.
   - Positions with imported risk (products core cannot price) contribute only what was imported, and are flagged.
5. **Scenario identity.**
   - A scenario definition is canonicalised and hashed.
   - T2 results are keyed by `hash(definition, base_run_id)`, so an unedited preset and an identical edited scenario share a result.
   - This fixes the mockups' issue where edited scenarios never match stored results.

## 4 What the backend publishes for T1 (payload)

Per position:

| Field group | Content | Size |
|---|---|---|
| Identity and grouping | ID, book, entity, instrument, issuer, sector, rating, currency, product, accounting category | small |
| Valuation | PV (RUB), clean/dirty, yield, spread, valuation source (`computed` / `imported`), coverage state | small |
| First order | KRD DV01 per (curve, bucket) present for the position; CS01 or OFZ-spread01; FX delta per currency | ~11–25 numbers |
| Second order | **Sparse, factor-keyed** Hessian entries `(i, j, d2PV)` over the factors the position actually has (`IR\|curve\|bucket`, `CS`, `FX\|ccy`); upper triangle only; with `greeks_as_of_run` | ~78 entries for a single-curve RUB bond (11 buckets + 1 spread); more for multi-curve |

- **Encoding:** factor names are sent once in a dictionary, and entries reference them by integer index. For 2k positions the whole risk payload is about 1–3 MB, which is fine to send to the browser.
- **The full Hessian replaces the tridiagonal assumption.** `DATA_CONTRACT.md` §4.3.3 assumed IR×IR was tridiagonal. Core computes a full 12×12 Hessian and documents that bootstrapped curves and settlement rebasing couple distant pillars (`rub-fixed-income-core/docs/second-order-local-risk.md`). Contract 1.4 should accept full sparse Hessians and drop the tridiagonal recommendation. The storage format (`position_gamma` upper triangle) already allows it.
- **Aggregated cubes for first paint.** The backend also publishes the default view's aggregates: book × curve × bucket DV01 and desk totals. The first screen therefore needs no client computation. Per-position data loads with it for filtering.

**Why per-position and not pre-aggregated?** Taylor PnL is linear in greeks only when the shock is the same for every position in a group. Credit shocks by rating, sector or issuer, and the spread multiplier, differ per position. Ad-hoc filters also cannot be pre-aggregated. At ≤2k positions, per-position greeks are cheap.

## 5 When exact is mandatory or suggested

The UI shows an accuracy state for each T1 result, computed from published metadata:

| Condition | UI behaviour |
|---|---|
| Positions under a shock where the effective move exceeds a configurable threshold (default 500 bp, as in the mockups; calibrate per product from T2 residuals) | Warning chip with the MV share affected; auto-run T2 |
| Positions with no second-order data (imported risk without gamma, products without a Hessian) | Info chip "first order only for X% MV" |
| Floaters with caps or floors, bonds with offers near the horizon, CDS / JTD-style scenarios | Auto-run T2 |
| The user saves, shares or exports a scenario | Always T2 |

- **Every T2 result displays its residual against T1**, both per book and in total. Stored residuals calibrate the threshold over time.
- Core claims **no universal error band**, so the threshold is a calibrated policy and not a guarantee.

## 6 Intraday

| Item | Intraday behaviour |
|---|---|
| PV and first-order greeks | Recomputed every intraday run (about 3 s for 2k bonds in zero-curve basis; OIS-quote basis needs curves shared per snapshot) |
| Second-order Hessian | Reused from the latest EOD run and labelled with its age |
| Attribution greeks (from M5) | **Default, pending DEC05:** frozen at the previous EOD for the whole day, so intraday PnL explain does not drift as current greeks change. DEC05 is still open |
| T1 base | The browser uses the greeks of the run on screen (EOD or intraday), as stated in the run badge |
| T2 base | The selected run. A job on an intraday base is labelled accordingly |
| Positions | As known at the run time. Without an intraday position feed, the UI says "opening book repriced" ([04](04_BACKEND.md) §6) |

## 7 Mapping to `SCENARIOS.md` levels and the mockups

| `SCENARIOS.md` | This document | Change |
|---|---|---|
| Level 1: browser Taylor | T1 | Now display-only and parity-tested; full sparse Hessian instead of tridiagonal |
| Level 2: nightly exact presets and historical | T3 | Results bound to definition hash, base run, snapshot and model version, with coverage |
| Level 3: on-demand exact via `scenario_job` | T2 | Built in the backend worker ([04](04_BACKEND.md) §5); auto-triggered by the rules in §5 |
| Carry in the browser (`SCENARIOS.md` §7, `desk-util.js runCarry`) | Backend synchronous carry endpoint | Moves to the backend; one implementation of funding and carry conventions |

Functions in `prototypes/desk-util.js` and where they go:

| Mockup function | Target |
|---|---|
| `runScenario`, `secondOrder` | T1 port of the backend Taylor engine |
| `storedResult` | Replaced by fetching T2/T3 results by hash, with coverage |
| `runCarry`, `ksPath`, `indexAt` | Backend carry service |
| `fundingFx`, OCP, issuer exposure | Backend per-position fields; the browser only sums |
| `spreadTo`, `curveRate` | Backend publishes spreads and curve points; the browser only displays |
| `gammaSummary` | Backend publishes summaries |

## 8 Performance prerequisites

- **Core:** builds each shocked curve once per job and shares it across positions, holding no global state.
  - **Delivered in 0.7.0** (`ShockedCurveCache`). On the core's synthetic 10-position OIS job with Hessian, time fell from about 27 s to 2.2 s. Before that, about 500 ms per position ([module review](reviews/2026-10-16_module_review.md), CORE-E).
  - **Remaining:** accept prebuilt base and shocked curves from the caller, so the backend's persistent cache spans jobs; a benchmark at 2k positions on Windows / Python 3.11.
  - Run positions in a process pool.
- **Backend:**
  - own the persistent curve cache ([ADR-004](adr/ADR-004-curves-built-in-backend.md)) and keep it warm after each publish;
  - run T2 jobs in a dedicated worker process;
  - share curve builds between T2 jobs on the same base run.
- **Benchmarks to record** before setting final budgets: EOD for the actual product mix; intraday run; T2 p95; carry call p95.
