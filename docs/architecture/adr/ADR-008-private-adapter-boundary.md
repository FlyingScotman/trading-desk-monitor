# ADR-008 Firm-specific report parsing lives in a private adapter package; the backend owns ingestion

Status: accepted (19 October 2026).

## Context

The desk's own data comes as bank reports dropped into folders on the work machine: a morning positions report, an OCP and cash report, the official PnL reports (T+1), and later intraday trade exports and internal EOD market data. Their layouts, field names and codes are proprietary. This repository is public, so nothing firm-specific may land here: no field names, file names, book codes or sample values.

[04](../04_BACKEND.md) §1 already plans `backend/private_inputs` as "declarative adapters for bank files". This ADR splits that module in two.

## Decision

1. **The public backend owns ingestion.** `desk.inbox` watches configured folders, hashes every file (SHA-256), keeps an ingest ledger (per file, and per dataset, business date and scope unit it touched), and stores accepted rows as bitemporal revisions ([04](../04_BACKEND.md) §2, [ADR-006](ADR-006-desk-store-postgres-bitemporal.md)). It raises events (dataset X for date D arrived, rejected, superseded) and starts the pipelines that depend on them.
2. **The backend defines canonical private datasets** in the private input contract v1 (`contract/private/v1/`, versioned on its own, [08](../08_PRIVATE_ADAPTER_INTERFACE.md) §9–§10): `positions`, `cash_balances`, `ocp`, `pnl_official` (with components and trade keys), `trades`, `internal_market` (an overlay in the shape of the market snapshot contract). Schemas, units, signs and keys are public; they say nothing about any firm.
3. **A private adapter package translates.** It lives in the private repository `trades-data-adapter` and sits anywhere on the work machine. The backend loads it from the path in `DESK_PRIVATE_ADAPTER` (a package directory, or a module name if installed); nothing is registered or installed into the backend. Its interface (SPI v1, [08](../08_PRIVATE_ADAPTER_INTERFACE.md)) is small and stateless: `describe()`, `match(path, ctx)` recognises a file and its report date, `read(path, hit, ctx)` returns canonical rows with their source location, issues and the report's own control totals, and `reference(ctx)` gives reference rows such as books (required when any source emits `book_id`).
4. **The public repository ships a reference adapter** (`desk.inbox.reference`) that reads canonical CSV files, and synthetic fixtures. Every backend test runs on it. The private repository tests its adapters on synthetic files built from report metadata, never on real reports.
5. **Format discovery goes through metadata only.** The private repository carries a local metadata profiler (successor of pnl_tool's `report_profiler`). The owner runs it at work and shares only its output: column types, null shares, cardinalities, masked patterns, key candidates, cross-report key overlap by locally salted hashes, file-name patterns and arrival times. No values leave the work machine unless the owner whitelists a low-cardinality column.

## Consequences

- One ingest ledger. The adapter runs in a child process without the store URL or the hub token, so it cannot write the store, and a hung adapter cannot stop the backend.
- A new report family is a new canonical dataset (public, generic) plus a parser (private).
- The backend runs without the private package (reference adapter only), so CI and the public demo keep working.
- A private contract change needs a matching adapter release; the adapter declares the SPI and contract versions, and the backend refuses an adapter it does not support (ingestion off, everything else working).
- The backend and the adapter are built in parallel against [08](../08_PRIVATE_ADAPTER_INTERFACE.md), the reference adapter and the conformance kit (`desk adapter check`); they meet at integration points I1–I6.
