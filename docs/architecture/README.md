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
| 6 | [06_DEPLOYMENT_AND_OPERATIONS.md](06_DEPLOYMENT_AND_OPERATIONS.md) | Windows server inside, one hub on a Linux VPS for several consumers: snapshot cache, proxy, offline install on Python 3.11, time zones, backups, monitoring |
| 7 | [07_CORE_PRODUCT_COVERAGE.md](07_CORE_PRODUCT_COVERAGE.md) | The development plan for products the core library cannot price yet. It is self-contained so it can be handed to the core project |
| 8 | [ROADMAP.md](ROADMAP.md) | Work by repository, sequencing, and open questions for the owner |
| 9 | [VERTICAL_SLICES.md](VERTICAL_SLICES.md) | **Current way of working:** thin end-to-end slices on real data (S1–S8; next S3 and S4), their shortcuts, results and findings |
| 10 | [briefs/HUB.md](briefs/HUB.md), [briefs/HUB_STATIC_DATA_REQUESTS.md](briefs/HUB_STATIC_DATA_REQUESTS.md), [briefs/CORE.md](briefs/CORE.md) | Work orders from the desk to the hub and to core: what blocks the desk, findings on real data, inputs core can use; static data requests (issuers, groups, sectors, ratings) from the R9 audit |
| — | [adr/](adr/) | Architecture decision records: one decision per file |
| — | [reviews/](reviews/) | Point-in-time reviews; not maintained |

## Components and repositories

| Component | Repository (local folder) | Role | Status (Oct 2026) |
|---|---|---|---|
| **Desk backend** | `trading_desk_monitor` (this repo) | The heart of the system. Orchestrates runs, prices with core, applies desk conventions, stores results, serves the desk API | Documents and contract 1.3; `tdm/` is a prototype of the store |
| **Frontend** | `trading_desk_monitor` (`prototypes/` today) | Screens for risk, PnL explain, scenarios, bonds | Visual mockups that define how the tool should look |
| **Market data hub** | `market-data-server` (package `mdhub`) | Collects, validates, versions and serves public market and static data | Deployed on the VPS (hub 0.1.0, 18 October 2026). Collection is mature; the snapshot layer is not merged or deployed yet |
| **Calculation core** | `rub-fixed-income-core` (package `rub_fixed_income`) | Pure pricing and risk library, with no I/O | v0.32.0 (18 October 2026): domain model, KBD, `value_portfolio`, horizon explain, and engines for every product of the book ([04](04_BACKEND.md) §7) |
| **PnL tool** | `pnl_tool` (local, no git) | Standalone accounting-PnL dashboard over the official trade-level PnL report | Standalone by design; to be connected later as the official-PnL source |

## Where documents live

**Rule: the system repo describes the whole system and indexes every interface. The producer of an interface owns its contract. A component's internals live in that component's repo.**

**Exception:** when the producer is outside the project (bank systems, the standalone pnl_tool), the consuming backend owns the contract and its templates. See [ADR-007](adr/ADR-007-repo-and-doc-layout.md).

| Kind of document | Lives in | Examples |
|---|---|---|
| System architecture, cross-component decisions, roadmap | `trading_desk_monitor/docs/architecture/` | This folder |
| Financial and organisational decisions (DEC01–DEC12) | `trading_desk_monitor/docs/program/DECISIONS.md` | Funding base, attribution order, market cut |
| Desk conventions and desk data contracts | `trading_desk_monitor/docs/` and `contract/` | `ORG_CONVENTIONS.md`, `DATA_CONTRACT.md`, `contract/bundle.schema.json`, `contract/csv/` |
| Market Snapshot Contract (schema plus golden fixtures) | `market-data-server` (`schemas/market_snapshot/v1/`, in a hub branch) | Produced by the hub, pinned by the backend |
| Market-data internals: sources, schema, jobs, quality | `market-data-server/docs/` | `DESIGN.md`, `STATUS.md` |
| Core domain model, conventions, API, numerical methods | `rub-fixed-income-core/docs/` | `ARCHITECTURE.md`, `CONVENTIONS.md`, `risk.md` |
| Core product development plan | **`rub-fixed-income-core/docs/PRODUCT_ROADMAP.md`** (maintained there); [07](07_CORE_PRODUCT_COVERAGE.md) is the frozen snapshot it came from | — |

### Entry points in each repository

Each repository has a short system-context file that states its role, its obligations and links back here. Paths are relative to the parent folder in the usual side-by-side checkout.

| Repository | Entry point | Also |
|---|---|---|
| trading_desk_monitor | `docs/architecture/README.md` (this file) | `docs/architecture/ROADMAP.md` |
| rub-fixed-income-core | `docs/SYSTEM_CONTEXT.md` (linked from its README) | `docs/PRODUCT_ROADMAP.md`: the core development plan |
| market-data-server | `docs/SYSTEM_CONTEXT.md` (linked from its README) | System work list for the hub: snapshot contract, VPS deploy for several consumers |
| pnl_tool (local, no git) | `SYSTEM_CONTEXT.md` | Planned role at M5 |

### Interface catalogue

Every boundary between components is listed here with its owner and the version the system currently pins. Update this table in the same change that bumps a pin.

| Interface | Producer → consumer | Owner | Format | Pinned version | Status |
|---|---|---|---|---|---|
| Market Snapshot Contract | hub → backend adapter | market-data-server | JSON Schema + golden fixtures | v1: every minor `1.x` is read (D10); 1.2 understood | The backend vendors the 1.0 golden fixtures ([02](02_MARKET_DATA_AND_CORE_CONTRACT.md)). **1.2** (hub 0.2.0, WEB-15, additive): BondQuote `source_analytics` `icpi`, `ir`, `bei`, `cbr`, `duration_waprice_days`, `yield_last_coupon_pct` (the desk stores the linkers' four in its market history and shows them on the bond card); Fixing `hub_ingested_at`; RUSFAR units: the rate lists `pct`, `RUSFARIND` `index` (the desk reads only `pct` rows as rates); stale reason `input not ready: …` on `bond_quotes` / `zcyc_params` (a cut built before its inputs arrived: shown as the dataset's stale reason) |
| Snapshot endpoints (`/v1/snapshots`, manifest, bundle) | hub on the VPS → backend snapshot cache, other consumers | market-data-server | HTTPS with a scoped bearer token; sha256-verified bundles | v1 | Built in a hub branch, not merged or deployed yet: the VPS hub answers 404 on `/v1/snapshots`. The backend client is slice S3 ([VERTICAL_SLICES](VERTICAL_SLICES.md)) |
| Hub read API (`/v1/...`) | hub → backend (market proxy, research) | market-data-server | HTTP JSON | unversioned | Exists; raw rows, no response models |
| Hub history API (`/v1/history/index\|bonds\|curves/{dataset}\|zcyc\|fixings\|fx`) | hub → backend market history | market-data-server (`docs/HISTORY_API.md`) | HTTPS, bearer token scope `read`; columnar envelope (`columns`, `rows`, `next`, `data_version`), keyset paging, weak ETag / 304, gzip, exact decimals | hub 0.2.0 | Client and sync in `marketdata/hub.py`, `marketdata/hubhistory.py` (R24). Production hub 0.1.3 answers 404: the desk falls back to the snapshots and says why («hub без API истории (версия < 0.2.0)», «токен без права read») |
| Core domain model and API | core → backend | rub-fixed-income-core | Python package; results `rub-portfolio-risk-result.v2` (bonds) / `.v3` (with derivatives), hash-verified JSON | 0.32.0, pinned by commit `c0af388` in `backend/pyproject.toml` (no release tags after `v0.3.0`) | The backend uses only `BondRiskEngine` so far; it moves to `value_portfolio` in S4 ([briefs/CORE.md](briefs/CORE.md)) |
| Run bundle and desk store contract | valuation engine → store | trading_desk_monitor | `contract/bundle.schema.json`, `docs/DATA_CONTRACT.md` | 1.3 | Exists |
| Private input files (positions, trades, OTC, funding, official PnL) | bank systems → backend | trading_desk_monitor | `contract/csv/*.csv` | 1.3 | Exists as templates |
| Imported risk for products not yet computable | bank systems → backend | trading_desk_monitor | contract 1.3 CSV / bundle | 1.3 | Interim path ([ADR-001](adr/ADR-001-hosting-topology.md), [07](07_CORE_PRODUCT_COVERAGE.md)) |
| Desk API | backend → frontend | trading_desk_monitor | OpenAPI + generated TypeScript types | v1 | **To be created** ([05](05_FRONTEND_INTEGRATION.md)) |
| Official PnL feed | pnl_tool reader → backend | trading_desk_monitor | `pnl_official` dataset | — | Later |
| Hub `dataset` → core curve-spec mapping | backend adapter → core | trading_desk_monitor | Versioned mapping table | — | **To be created** ([02](02_MARKET_DATA_AND_CORE_CONTRACT.md) §4) |
| Taylor parity fixtures | backend reference engine → frontend T1 port | trading_desk_monitor | JSON fixtures generated in Python | — | **To be created** ([03](03_CALCULATION_SPLIT.md) §3) |

## Relation to older documents

- `docs/program/`: the execution pack of 14 October 2026. It assumed an external VPS for market data and a Windows work PC for calculations, with an LLM data curator. [ADR-001](adr/ADR-001-hosting-topology.md) supersedes the work-PC, Hermes and bulk-download parts. The Linux VPS remains for public collection: one hub there serves the inside backend, which pulls snapshots into its cache, and the owner's other consumers (amended 18 October 2026). `DECISIONS.md` DEC01–DEC12 remain valid and authoritative for financial choices.
- `docs/DATA_CONTRACT.md`, `ORG_CONVENTIONS.md`, `SCENARIOS.md`, `DATA_REQUIREMENTS.md`, `SERVICE_DATA_MODEL.md`: remain the detailed desk specifications. Where this folder changes a design, for example the scenario levels or gamma sparsity, the change is stated explicitly and those files get a pointer.
- [reviews/2026-10-16_module_review.md](reviews/2026-10-16_module_review.md): the module-by-module review that preceded this folder. Read its header note first.
- [reviews/STATIC_DATA_AUDIT_2026-10-20.md](reviews/STATIC_DATA_AUDIT_2026-10-20.md): audit of the bond static data as the desk sees it (R9), with `desk instruments export`.

## Precedence

When documents disagree, use this order:

1. Confirmed financial rules in `ORG_CONVENTIONS.md` and accepted DEC entries.
2. ADRs in this folder. An **accepted** ADR binds. A **proposed** ADR is the working default that implementation follows until the owner accepts, amends or rejects it. Record the outcome in the ADR's status line.
3. The numbered documents in this folder.
4. The older detailed specifications.
5. Code.

An open DEC entry stays open even when a document or a prototype picks a default.
