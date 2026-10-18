# ADR-002 Immutable, materialized market snapshots; schema owned by the hub

Status: proposed (16 October 2026).

## Context

Today the hub serves raw table rows over `/v1` and builds the TDM CSV export on demand. The export is regenerated on each call. There is no snapshot identity, and as-known-at reads exist only for bond EOD (`md.bond_eod*_known_at`) and `md.fixing`. Calendars, swap-quote sets, ZCYC parameters, FX and bond terms cannot be replayed for a past moment. A valuation run therefore cannot pin, or later reproduce, the market data it used.

## Decision

1. The hub **publishes immutable snapshots**. Each is written once as canonical JSON (gzipped), recorded in a published-snapshot table (for example `ops.published_snapshot`; distinct from the existing per-SECID `ref.static_snapshot` payload cache), and never rebuilt from live tables.
   - **Static snapshot:** instruments, schedules (coupons with status, amortizations, offers), floater formulas, issuers, calendars and identifier aliases. Published when static data changes, at least daily.
   - **Market snapshot:** bond prices (all price bases, labelled), curve quote sets, ZCYC/KBD parameters, fixings, FX (CBR official, MOEX, indicative). Published at each cut: EOD (and final next-morning) and intraday every N minutes. Each market snapshot names the `static_snapshot_id` it is consistent with.
   - Snapshot ID = sha256 of the canonical payload. The manifest carries `schema_version`, `kind`, `cut_type`, `business_date` (Moscow), `cut_ts` (UTC), `known_at`, sources, per-dataset counts, coverage, stale flags and `supersedes`.
2. The **wire schema is owned by the hub**, the producer: `market-data-server/schemas/market_snapshot/v1/` (JSON Schema) plus golden fixtures, versioned with hub releases. Consumers pin a version and run conformance tests against the fixtures.
3. The hub publishes **facts and labels it actually has** (its `dataset` codes, tenor codes, units, coupon status, price basis). It does not invent conventions it does not have. Conventions belong to core's curve-spec registry, keyed by core's own spec IDs. The backend adapter maps hub `dataset` codes to them ([ADR-003](ADR-003-adapter-in-backend.md), [ADR-004](ADR-004-curves-built-in-backend.md)).
4. The TDM CSV export stays as a debugging/compatibility path and is retired once the backend reads snapshots.

## Consequences

- Runs become reproducible by snapshot ID. Corrections produce a new snapshot that supersedes the old one, and the old one stays readable.
- The hub needs:
  - a snapshot builder and publisher;
  - as-known-at readers for static tables (`ref.*`) over `ops.row_history`, and versioned changes instead of the hard deletes in `ref.calendar_day` and `ref.weekend_session`;
  - list/manifest/bundle endpoints;
  - retention rules.
- Snapshots are the unit that crosses from the hub on the VPS into the work network ([ADR-001](ADR-001-hosting-topology.md)). The desk backend pulls them into its snapshot cache and verifies the sha256 against the ID; content-hash IDs make the pull idempotent. Other consumers of the hub read the same snapshots with their own tokens.
- Storage grows with intraday cuts. Keep intraday market snapshots small by referencing the static snapshot rather than embedding it.
