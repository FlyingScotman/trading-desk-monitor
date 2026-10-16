# Trading Desk Monitor: architecture

Revision 1, 16 October 2026. This folder is the system-level architecture of the Trading Desk Monitor. It holds the components, how they talk to each other, who owns which contract, and the order of work. Component internals are documented in each component's own repository.

## Reading order

| # | Document | Answers |
|---|---|---|
| 1 | [01_SYSTEM_OVERVIEW.md](01_SYSTEM_OVERVIEW.md) | What the system is, its components, deployment, the main data flows, and the honest state of each part today |
| 2 | [02_MARKET_DATA_AND_CORE_CONTRACT.md](02_MARKET_DATA_AND_CORE_CONTRACT.md) | How the calculation library gets market and static data: layering, the Market Snapshot Contract, who builds curves and projects cashflows |
| 3 | [03_CALCULATION_SPLIT.md](03_CALCULATION_SPLIT.md) | Which calculations run in the backend and which in the browser, including interactive stress scenarios and intraday |
| 4 | [04_BACKEND.md](04_BACKEND.md) | What the desk backend is made of: orchestration, valuation, desk conventions, store, run lineage, jobs |
| 5 | [05_FRONTEND_INTEGRATION.md](05_FRONTEND_INTEGRATION.md) | How the frontend is fed by the backend: the desk API, events, types, auth, the transition from the mockups |
| 6 | [06_DEPLOYMENT_AND_OPERATIONS.md](06_DEPLOYMENT_AND_OPERATIONS.md) | Windows server inside, hub split with a Linux VPS: snapshot replication, proxy, offline install on Python 3.11, time zones, backups, monitoring |
| 7 | [07_CORE_PRODUCT_COVERAGE.md](07_CORE_PRODUCT_COVERAGE.md) | The development plan for products the core library cannot price yet. It is self-contained so it can be handed to the core project |
| 8 | [ROADMAP.md](ROADMAP.md) | Work by repository, sequencing, and open questions for the owner |
| — | [adr/](adr/) | Architecture decision records: one decision per file |
| — | [reviews/](reviews/) | Point-in-time reviews; not maintained |

## Components and repositories

| Component | Repository (local folder) | Role | Status (Oct 2026) |
|---|---|---|---|
| **Desk backend** | `trading_desk_monitor` (this repo) | The heart of the system. Orchestrates runs, prices with core, applies desk conventions, stores results, serves the desk API | Documents and contract 1.3; `tdm/` is a prototype of the store |
| **Frontend** | `trading_desk_monitor` (`prototypes/` today) | Screens for risk, PnL explain, scenarios, bonds | Visual mockups that define how the tool should look |
| **Market data hub** | `market-data-server` (package `mdhub`) | Collects, validates, versions and serves public market and static data | Under active development; collection is mature, dispatch is minimal |
| **Calculation core** | `rub-fixed-income-core` (package `rub_fixed_income`) | Pure pricing and risk library, with no I/O | v0.6.0: fixed-coupon RUB bonds only |
| **PnL tool** | `pnl_tool` (local, no git) | Standalone accounting-PnL dashboard over the bank's TradeLevel report | Standalone by design; to be connected later as the official-PnL source |

## Where documents live

**Rule: the system repo describes the whole system and indexes every interface. The producer of an interface owns its contract. A component's internals live in that component's repo.**

**Exception:** when the producer is outside the project (bank systems, the standalone pnl_tool), the consuming backend owns the contract and its templates. See [ADR-007](adr/ADR-007-repo-and-doc-layout.md).

| Kind of document | Lives in | Examples |
|---|---|---|
| System architecture, cross-component decisions, roadmap | `trading_desk_monitor/docs/architecture/` | This folder |
| Financial and organisational decisions (DEC01–DEC12) | `trading_desk_monitor/docs/program/DECISIONS.md` | Funding base, attribution order, market cut |
| Desk conventions and desk data contracts | `trading_desk_monitor/docs/` and `contract/` | `ORG_CONVENTIONS.md`, `DATA_CONTRACT.md`, `contract/bundle.schema.json`, `contract/csv/` |
| Market Snapshot Contract (schema plus golden fixtures) | `market-data-server` (`schemas/market_snapshot/v1/`, to be created) | Produced by the hub, pinned by the backend |
| Market-data internals: sources, schema, jobs, quality | `market-data-server/docs/` | `DESIGN.md`, `STATUS.md` |
| Core domain model, conventions, API, numerical methods | `rub-fixed-income-core/docs/` | `ARCHITECTURE.md`, `CONVENTIONS.md`, `risk.md` |
| Core product development plan | **`rub-fixed-income-core/docs/PRODUCT_ROADMAP.md`** (maintained there); [07](07_CORE_PRODUCT_COVERAGE.md) is the frozen snapshot it came from | — |

### Entry points in each repository

Each repository has a short system-context file that states its role, its obligations and links back here. Paths are relative to the parent folder in the usual side-by-side checkout.

| Repository | Entry point | Also |
|---|---|---|
| trading_desk_monitor | `docs/architecture/README.md` (this file) | `docs/architecture/ROADMAP.md` |
| rub-fixed-income-core | `docs/SYSTEM_CONTEXT.md` (linked from its README) | `docs/PRODUCT_ROADMAP.md`: the core development plan |
| market-data-server | `docs/SYSTEM_CONTEXT.md` (linked from its README) | System work list for the hub: snapshot contract, replica, Windows part |
| pnl_tool (local, no git) | `SYSTEM_CONTEXT.md` | Planned role at M5 |

### Interface catalogue

Every boundary between components is listed here with its owner and the version the system currently pins. Update this table in the same change that bumps a pin.

| Interface | Producer → consumer | Owner | Format | Pinned version | Status |
|---|---|---|---|---|---|
| Market Snapshot Contract | hub → backend adapter | market-data-server | JSON Schema + golden fixtures | v1 | **To be created** ([02](02_MARKET_DATA_AND_CORE_CONTRACT.md)) |
| Snapshot replication | hub VPS → hub inside | market-data-server | Snapshot list/manifest/bundle endpoints, sha256-verified | v1 | **To be created** ([06](06_DEPLOYMENT_AND_OPERATIONS.md) §1) |
| Hub read API (`/v1/...`) | hub → backend (market proxy, research) | market-data-server | HTTP JSON | unversioned | Exists; raw rows, no response models |
| Core domain model and API | core → backend | rub-fixed-income-core | Python package | 0.6.0 today | Needs domain types ([07](07_CORE_PRODUCT_COVERAGE.md) package 0) |
| Run bundle and desk store contract | valuation engine → store | trading_desk_monitor | `contract/bundle.schema.json`, `docs/DATA_CONTRACT.md` | 1.3 | Exists |
| Private input files (positions, trades, OTC, funding, official PnL) | bank systems → backend | trading_desk_monitor | `contract/csv/*.csv` | 1.3 | Exists as templates |
| Imported risk for products core cannot price | bank systems → backend | trading_desk_monitor | contract 1.3 CSV / bundle | 1.3 | Interim path ([ADR-001](adr/ADR-001-hosting-topology.md), [07](07_CORE_PRODUCT_COVERAGE.md)) |
| Desk API | backend → frontend | trading_desk_monitor | OpenAPI + generated TypeScript types | v1 | **To be created** ([05](05_FRONTEND_INTEGRATION.md)) |
| Official PnL feed | pnl_tool reader → backend | trading_desk_monitor | `pnl_official` dataset | — | Later |
| Hub `dataset` → core curve-spec mapping | backend adapter → core | trading_desk_monitor | Versioned mapping table | — | **To be created** ([02](02_MARKET_DATA_AND_CORE_CONTRACT.md) §4) |
| Taylor parity fixtures | backend reference engine → frontend T1 port | trading_desk_monitor | JSON fixtures generated in Python | — | **To be created** ([03](03_CALCULATION_SPLIT.md) §3) |

## Relation to older documents

- `docs/program/`: the execution pack of 14 October 2026. It assumed an external VPS for market data and a Windows work PC for calculations, with an LLM data curator. [ADR-001](adr/ADR-001-hosting-topology.md) supersedes the work-PC, Hermes and bulk-download parts. The Linux VPS remains for public collection, now linked to the inside server by snapshot replication. `DECISIONS.md` DEC01–DEC12 remain valid and authoritative for financial choices.
- `docs/DATA_CONTRACT.md`, `ORG_CONVENTIONS.md`, `SCENARIOS.md`, `DATA_REQUIREMENTS.md`, `SERVICE_DATA_MODEL.md`: remain the detailed desk specifications. Where this folder changes a design, for example the scenario levels or gamma sparsity, the change is stated explicitly and those files get a pointer.
- [reviews/2026-10-16_module_review.md](reviews/2026-10-16_module_review.md): the module-by-module review that preceded this folder. Read its header note first.

## Precedence

When documents disagree, use this order:

1. Confirmed financial rules in `ORG_CONVENTIONS.md` and accepted DEC entries.
2. ADRs in this folder. An **accepted** ADR binds. A **proposed** ADR is the working default that implementation follows until the owner accepts, amends or rejects it. Record the outcome in the ADR's status line.
3. The numbered documents in this folder.
4. The older detailed specifications.
5. Code.

An open DEC entry stays open even when a document or a prototype picks a default.
