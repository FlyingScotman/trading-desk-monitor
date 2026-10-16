# ADR-005 Calculation split: backend authoritative, browser interactive

Status: proposed (16 October 2026). Details in [03_CALCULATION_SPLIT.md](../03_CALCULATION_SPLIT.md). Refines `docs/SCENARIOS.md` levels 1–3.

## Context

The owner asked whether new calculations, such as changing a stress scenario, should be done by the backend, or whether the backend should compute snapshot risk (first- and second-order) and leave all further calculation to the browser.

## Decision

| Tier | Where | What | Persisted |
|---|---|---|---|
| T3 batch | Backend, EOD and intraday runs | PV and first-order risk every run; second-order (Hessian) at EOD; exact preset and historical scenarios; carry/funding projections | Yes |
| T1 instant | Browser, while editing | Second-order Taylor approximation from published sparse per-position greeks; labelled ≈ | **Never** |
| T2 exact | Backend job, on demand | Full revaluation of the base run under the edited scenario, keyed by `hash(definition, run_id)`; returns exact result and its residual against T1 | Yes |

Rules:
1. Any number that is stored, shared, exported, reported or reconciled is computed by the backend. Browser numbers are display-only.
2. The browser filters and sums backend-computed per-position fields through one shared selectors module. It never derives official PnL, breaks, OCP, funding or carry.
3. The browser Taylor engine is a port of the Python implementation and is parity-tested against fixtures generated in Python.
4. Carry and funding projections move from the browser to the backend.

## Consequences

- No duplicated business logic, apart from the tested Taylor port.
- Instant interaction is preserved, and exact numbers should arrive within seconds for about 2k positions. This requires shared shocked-curve builds: call-level reuse in core plus the backend's persistent cache ([ADR-004](ADR-004-curves-built-in-backend.md)). Budgets are provisional until benchmarked.
- The data contract's tridiagonal gamma (`DATA_CONTRACT.md` §4.3.3) is replaced by sparse factor-keyed Hessian entries emitted by core ([03](../03_CALCULATION_SPLIT.md) §4).
