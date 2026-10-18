# Roadmap

Revision 2, 18 October 2026. This is a sequencing view. Each repository keeps its own detailed task state in its own `STATUS.md`. `docs/program/backlog.json` is historical and is not reconciled with this file.

## 1 Milestones

The order of work is set by [VERTICAL_SLICES.md](VERTICAL_SLICES.md): thin slices on real data that reach these milestones step by step. Slice S1 (17.10.2026) covers most of M1, with shortcuts listed there.

| Milestone | Outcome | Needs |
|---|---|---|
| **M0 Foundations** | Contracts written and pinned; Windows/VPS topology and proxy facts known | Q4 answered (§3) |
| **M1 Vertical slice (R0)** | Synthetic fixed-coupon OFZ book discounted on the KBD/ZCYC curve: hub snapshot → backend adapter → core → desk store → desk API → one migrated screen (Desk Monitor summary and positions). Run lineage recorded; coverage shown; **flash PnL only** | Hub snapshots; core packages 0.1–0.4 and 0.6 (KBD evaluator), plus a minimal 0.5 registry entry `rub_ofz_kbd_v1`; backend skeleton; React + TypeScript frontend scaffold |
| **M2 Desk EOD** | Real EOD run on the server for supported bonds plus imported risk for the rest; desk rules (funding, OCP, dual FX, HTM); T3 presets and historical scenarios; carry endpoint; flash PnL | Position files: Q1 is deferred, so M2 runs on contract 1.3 position files prepared by hand or from a synthetic book until the source is decided; DEC01, DEC02, DEC03, DEC04, DEC06; core 0.5–0.8 |
| **M3 Interactive scenarios** | T1 parity-tested Taylor engine in the browser; T2 job worker; Scenario Lab migrated | Shared shocked curves: delivered in core 0.7.0 within a job; core still needs to accept prebuilt curves for the backend's persistent cache |
| **M4 Intraday** | Intraday hub snapshots; intraday flash runs (PV, first-order risk, flash PnL); SSE live updates | Positions feed decision (Q1); otherwise intraday reprices the opening book |
| **M5 PnL explain and official** | `backend/pnl` explain, EOD and intraday, with attribution greeks per DEC05; pnl_tool reader feeding `pnl_official` as append-only revisions; break register | DEC05, DEC11, DEC12; official PnL = TradeLevel IFRS (Q8, answered) |
| **M6 Coverage waves** | Core packages move products from `imported` to `computed`. Already in core 0.8.1: plain floaters and FX futures. Next: floater caps/floors and two-curve, IRS, non-RUB bonds, FX forwards/swaps | Core `docs/PRODUCT_ROADMAP.md` |
| **M7 Operate** | Alerting, off-host backups, restore drill, runbooks, remaining screens migrated, `window.DESK` adapter removed | — |

## 2 Work by repository

### market-data-server (hub)

These items are for the session or project working on the hub. They are also listed in `market-data-server/docs/SYSTEM_CONTEXT.md`. Do not interrupt work in progress; queue them after the current seed and backfill checkpoint.

1. **Market Snapshot Contract v1:** `schemas/market_snapshot/v1/` plus golden fixtures ([02](02_MARKET_DATA_AND_CORE_CONTRACT.md) §3, §6).
2. **As-known-at readers for static data:** `ref.*` over `ops.row_history`, deletes versioned.
3. **Snapshot builder and published-snapshot table** (distinct from the existing `ref.static_snapshot`): EOD preliminary and final cuts, intraday cuts; manifest; endpoints for list, manifest and bundle.
4. **Coverage labelling instead of exclusion:**
   - `coupon_type='unknown'` and perpetuals included with labels;
   - quotes limited to instruments in the snapshot;
   - floater benchmarks with formula status.
5. **One calendar version and fingerprint per snapshot.**
6. **Alerting** (failed or missed jobs, stale data, unfillable gaps, missed snapshot) and **backups off the database disk**.
7. **First VPS deployment for several consumers** ([ADR-001](adr/ADR-001-hosting-topology.md), hub `deploy/README_DEPLOY.md`). **Deployed 18 October 2026** (hub 0.1.0) without the snapshot layer; the remaining steps are H1–H5 of slice S3 ([VERTICAL_SLICES](VERTICAL_SLICES.md)):
   - the snapshot layer merged and deployed, with read grants for the API's database role;
   - a provider and region from which every public source answers (source probes first);
   - TLS, firewall, service database roles, off-site backups;
   - one named token per consumer with the narrowest scope (`snapshots` for the desk backend).
8. Later: an intraday API for bonds and FX; SOFR/€STR series as needed by core package 3. Bank-internal sources are not hub work: they enter through the backend's private-input adapters.

### rub-fixed-income-core

The plan is dispatched: `rub-fixed-income-core/docs/PRODUCT_ROADMAP.md` (from [07](07_CORE_PRODUCT_COVERAGE.md)); the core's role is in its `docs/SYSTEM_CONTEXT.md`. Housekeeping first:
- commit the pending `.gitignore` that ignores `audit/` and `implementation/`, and move those 3 GB evidence folders out of the checkout;
- tag the releases since 0.3.0 (only `v0.3.0` exists remotely; 0.4.0–0.8.1 are untagged);
- remove the proprietary reconciliation probe from the sdist;
- refresh `API.md`, `ARCHITECTURE.md` and `CONSUMERS.md`.

### trading_desk_monitor: backend

1. Skeleton `backend/` package: FastAPI app, Postgres `desk` migrations, orchestrator CLI, worker ([04](04_BACKEND.md)).
2. Store: runs with lineage, bitemporal private revisions, attachments, curve cache, jobs ([ADR-006](adr/ADR-006-desk-store-postgres-bitemporal.md)).
3. `marketdata`: hub client, snapshot cache and drop-folder import (slice S3); `adapters/mdhub_v1` and conformance tests on the hub fixtures (done in S1). Settings: `desk.settings`, `backend/.env.example`.
4. `private_inputs`: declarative CSV/XLSX adapters for contract 1.3 datasets, including imported risk.
5. `valuation`: core integration, hybrid merge, factor emission.
6. `desk_rules`: funding/OCP/FX/HTM/carry from `ORG_CONVENTIONS.md`, configured by the DEC entries.
7. `scenarios`: definitions and hashing, T3 batch, T2 worker, reference Taylor engine plus parity fixture generator.
8. Desk API v1 and SSE ([05](05_FRONTEND_INTEGRATION.md) §2); `window.DESK` compatibility endpoint.
9. Contract 1.4: full sparse Hessian (replacing the tridiagonal recommendation), coverage and provenance states, run lineage fields.

### trading_desk_monitor: frontend

1. TypeScript + Vite scaffold; generated API types; `selectors/`; locally bundled fonts.
2. `taylor/` port with parity fixtures.
3. Migrate Desk Monitor, then Scenario & PnL Lab, Pivot Studio, Widget Board, Bonds ([05](05_FRONTEND_INTEGRATION.md) §4).
4. Exports, saved views on the server, run badge with age and coverage, live updates.

### pnl_tool

No changes for now. At M5 extract its XLSX reader and snapshot-revision logic into a TradeLevel → `pnl_official` adapter. Decide whether to put its code under local git (it has no version control today).

## 3 Owner answers and open questions

| ID | Question | Status (16 October 2026) | Consequence |
|---|---|---|---|
| Q1 | Where do positions and trades come from? Is there an intraday trade feed? | **Deferred** (to be decided later) | Backend designs a contract 1.3 file interface only; intraday reprices the opening book until a feed exists |
| Q2 | Server OS and tooling | **Answered:** Windows inside the work network; Linux on the VPS | [ADR-001](adr/ADR-001-hosting-topology.md), [06](06_DEPLOYMENT_AND_OPERATIONS.md): Windows services; the backend pulls snapshots from the VPS hub into its cache |
| Q2a | Final split of hub collectors between VPS and inside | **Answered (18 October 2026):** one hub on the VPS, no inside instance; it serves several consumers | Inside-only data enters through the backend's private-input adapters ([06](06_DEPLOYMENT_AND_OPERATIONS.md) §4) |
| Q3 | Data licensing | **Answered:** all sources properly licensed | Record licence scope per source in the hub's source registry |
| Q4 | Proxy facts: TLS inspection CA, authentication type, allowlist process | **Open** | Blocks the snapshot pull from inside; the drop folder is the fallback |
| Q6 | Book mix by product | **Answered:** all listed products are present (fixed, floaters, OFZ-PK, IRS, FX, XCCY, CDS, linkers, perpetuals, non-RUB) | All core packages 1–8 are required; package 9 per DEC10; default order in [07](07_CORE_PRODUCT_COVERAGE.md) stands unless MV weights say otherwise |
| Q7 | Python version | **Answered:** 3.11 on the work machine | Windows side pinned to 3.11; core and the backend fit; the hub runs only on the VPS (Python 3.12) |
| Q8 | Is TradeLevel IFRS the official PnL (CBR basis, including funding)? | **Answered: yes** | `pnl_official` is fed from TradeLevel IFRS via pnl_tool's reader at M5; explain reconciles to it on the CBR basis, including funding |
| Q9 | Frontend framework | **Answered:** React + TypeScript (recommended stack) | [05](05_FRONTEND_INTEGRATION.md) §3 |

**Financial decisions on the critical path** (from `docs/program/DECISIONS.md`):
- DEC01: funding base and trade timing (M2);
- DEC02: accrual bases and calendars (M2);
- DEC03: REPO cash-leg funding (M2, if the book has REPO);
- DEC04: authoritative market cuts (M2);
- DEC06: OCP transfer convention (M2);
- DEC05: PnL attribution (M5);
- DEC08: numerical conventions, DV01 interpretation (M1–M3);
- DEC12: break thresholds (M5).
