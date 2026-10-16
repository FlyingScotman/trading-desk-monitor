# ADR-003 The wire → domain adapter lives in the backend; core stays wire-agnostic

Status: proposed (16 October 2026).

## Context

The owner asked whether the calculation library should "import the data collected by the market-data service". Today core has no dependency on the hub. Its only bridge, `rub_fixed_income/public_schedule.py` (`bind_market_data_store_schedule`), validates the response shape of the retired market-data-store's `get_projected_bond_cashflows`. The new hub does not produce that shape.

## Decision

Three layers, three owners:

| Layer | Owner | Content |
|---|---|---|
| Wire contract | hub | Market Snapshot Contract v1 (JSON Schema + fixtures), [ADR-002](ADR-002-materialized-market-snapshots.md) |
| Domain model | core | Typed, immutable `Instrument`, `MarketSnapshot`, `Position`, curve specs, conventions, with `to_dict`/`from_dict` of **its own** types |
| Adapter | backend (`adapters/mdhub_v1`) | Reads the pinned wire version and builds core domain objects. Owns coupon-status semantics, price-basis selection, unit scaling (percent → decimal in exactly one place), the SECID/ISIN map, the calendar version, and the versioned mapping from hub `dataset` codes to core curve-spec IDs. Applies private overrides with recorded precedence. |

Core never imports hub code or parses hub payloads. The hub never imports core. `bind_market_data_store_schedule` is deprecated and removed after the adapter exists.

## Consequences

- Core stays reusable with synthetic data, manual overrides, or another data source, and does not change in lockstep with the deployment format.
- One place to test the mapping: backend conformance tests run the hub's golden fixtures through the adapter into core and compare against expected domain objects and prices.
- A hub schema change touches the hub (schema plus fixtures) and the backend adapter, never core.
