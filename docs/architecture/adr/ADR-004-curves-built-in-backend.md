# ADR-004 Curves and cashflow projections are built in the backend with core

Status: proposed (16 October 2026). Resolves open question A5 of the module review.

## Context

The retired market-data-store built curves and projected floater cashflows inside the data service. The new hub's `DESIGN.md` explicitly lists curve bootstrapping, projection, spreads and pricing as non-goals. Being on one server removes any network reason to build curves in the hub. Projection depends on curves, so both must live in the same place.

## Decision

1. **The math is in core:**
   - the curve-spec registry, keyed by core-owned spec IDs (day count, payment frequency, spot and payment lag, compounding/averaging, interpolation);
   - bootstraps;
   - the KBD/ZCYC parametric evaluator;
   - floater coupon projection from formula, known fixings and projection curves.
2. **Orchestration and the persistent cache are in the backend:**
   - Build curves for each market snapshot and cache them, including shocked curves, keyed by `(snapshot_id, curve_spec_id, bump_spec, core_version)` in the desk database.
   - Historical scenarios, replays and on-demand jobs reuse the cache.
   - Core holds no global state. It offers a pure API that, within one call, builds each shocked curve once and shares it across positions, and it accepts prebuilt curves. That call-level reuse plus the backend's persistent cache solves the per-position re-bootstrap cost.
3. **Coupon policy:**
   - Use the hub's `known` coupons as facts.
   - Project `current_published`, `projected_by_moex` and `unknown` periods with core **only when the bond has a usable `FloaterFormula`** (status `curated` or `inferred`).
   - Otherwise reject the bond (for example issuer-set future coupons on fixed or `variable_known` bonds, or the about 668 bonds labelled `unknown`). Treat its position as `imported` or `not covered`. Never price it with guessed coupons.
   - Use MOEX's projected values only to reconcile and alert, never as pricing inputs.
4. **Analytics are labelled.** Built curves and computed spreads are stored as analytics with their spec and version, distinct from the hub's published observations.

## Consequences

- The hub stays data-only and simple.
- The backend needs a curve-build job and cache. Core needs the registry, bootstraps conforming to exchange specifications, and the projection API ([07](../07_CORE_PRODUCT_COVERAGE.md) package 0 and 1).
- If other consumers later need built curves, the backend can expose them read-only. Do not move building into the hub.
