# Roadmap

Revision 1, 16 October 2026. This is a sequencing view. Each repository keeps its own detailed task state in its own `STATUS.md`. `docs/program/backlog.json` is historical and is not reconciled with this file.

## 1 Milestones

| Milestone | Outcome | Needs |
|---|---|---|
| **M0 Foundations** | Contracts written and pinned; repository and accounts settled; server and proxy facts known | Q1–Q5 answered (§3) |
| **M1 Vertical slice (R0)** | Synthetic fixed-coupon OFZ book discounted on the KBD/ZCYC curve: hub snapshot → backend adapter → core → desk store → desk API → one migrated screen (Desk Monitor summary and positions). Run lineage recorded; coverage shown; **flash PnL only** | Hub snapshots; core packages 0.1–0.4 and 0.6 (KBD evaluator), plus a minimal 0.5 registry entry `rub_ofz_kbd_v1`; backend skeleton; Q9 (frontend stack) |
| **M2 Desk EOD** | Real EOD run on the server for supported bonds plus imported risk for the rest; desk rules (funding, OCP, dual FX, HTM); T3 presets and historical scenarios; carry endpoint; flash PnL | Q1 (positions); DEC01, DEC02, DEC03, DEC04, DEC06; core 0.5–0.8 |
| **M3 Interactive scenarios** | T1 parity-tested Taylor engine in the browser; T2 job worker; Scenario Lab migrated | Core 0.7 (shared shocked curves) plus the backend's persistent curve cache |
| **M4 Intraday** | Intraday hub snapshots; intraday flash runs (PV, first-order risk, flash PnL); SSE live updates | Positions feed decision (Q1); intraday licensing (Q3) |
| **M5 PnL explain and official** | `backend/pnl` explain, EOD and intraday, with attribution greeks per DEC05; pnl_tool reader feeding `pnl_official` as append-only revisions; break register | DEC05, DEC11, DEC12; Q8 (official PnL definition) |
| **M6 Coverage waves** | Core packages 1–4 move floaters, IRS, non-RUB and FX from `imported` to `computed` | [07](07_CORE_PRODUCT_COVERAGE.md) |
| **M7 Operate** | Alerting, off-host backups, restore drill, runbooks, remaining screens migrated, `window.DESK` adapter removed | — |

## 2 Work by repository

### market-data-server (hub)

These items are for the session or project working on the hub. Do not interrupt work in progress; queue them after the current seed and backfill checkpoint.

1. **Market Snapshot Contract v1:** `schemas/market_snapshot/v1/` plus golden fixtures ([02](02_MARKET_DATA_AND_CORE_CONTRACT.md) §3, §6).
2. **As-known-at readers for static data:** `ref.*` over `ops.row_history`, deletes versioned.
3. **Snapshot builder and published-snapshot table** (distinct from the existing `ref.static_snapshot`): EOD preliminary and final cuts, intraday cuts; manifest; endpoints for list, manifest and bundle.
4. **Coverage labelling instead of exclusion:**
   - `coupon_type='unknown'` and perpetuals included with labels;
   - quotes limited to instruments in the snapshot;
   - floater benchmarks with formula status.
5. **One calendar version and fingerprint per snapshot.**
6. **Alerting** (failed or missed jobs, stale data, unfillable gaps, missed snapshot) and **backups off the database disk**.
7. **Proxy-ready HTTP client:** custom CA bundle, proxy settings; a manual file-drop ingest path ([06](06_DEPLOYMENT_AND_OPERATIONS.md) §3).
8. Later: an intraday API for bonds and FX; internal bank sources as collectors; SOFR/€STR/CPI series as needed by core packages 3 and 9.

### rub-fixed-income-core

Dispatch [07_CORE_PRODUCT_COVERAGE.md](07_CORE_PRODUCT_COVERAGE.md) as written. Housekeeping first:
- commit the pending `.gitignore` that ignores `audit/` and `implementation/`, and move those 3 GB evidence folders out of the checkout;
- tag 0.4.0–0.6.0;
- remove the proprietary reconciliation probe from the sdist;
- refresh `API.md`, `ARCHITECTURE.md` and `CONSUMERS.md`.

### trading_desk_monitor: backend

1. Skeleton `backend/` package: FastAPI app, Postgres `desk` migrations, orchestrator CLI, worker ([04](04_BACKEND.md)).
2. Store: runs with lineage, bitemporal private revisions, attachments, curve cache, jobs ([ADR-006](adr/ADR-006-desk-store-postgres-bitemporal.md)).
3. `marketdata`: hub client and `adapters/mdhub_v1`, conformance tests on the hub fixtures.
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

## 3 Open questions for the owner

| ID | Question | Why it matters | Blocks |
|---|---|---|---|
| Q1 | **Where do positions and trades come from?** Which system, extract format, frequency; is there an intraday trade feed? | Without it the backend has no book; intraday means "opening book repriced" | M2, M4 |
| Q2 | **Server OS and allowed tooling** (Linux/Windows, Docker, systemd, Postgres) | Deployment shape | M2 |
| Q3 | **Data licensing** for MOEX ISS/APIM (including delayed intraday), Ratescalcs, Cbonds inside the bank; are internal vendor feeds available? | Legality, and possibly better sources | M2, M4 |
| Q4 | **Proxy facts:** TLS inspection CA, authentication type, host allowlist process | Hub connectivity | M2 |
| Q5 | **Canonical GitHub account**. This repo now exists in both FlyingScotman (`origin`) and FiveLimaGolf (`flg`); core and hub are in FiveLimaGolf | Pins, CI, cross-repo work | M0 |
| Q6 | **Book mix by product** (counts and MV: fixed, floaters, OFZ-PK, IRS, FX, XCCY, CDS, linkers, perpetuals, non-RUB) | Order of core packages | M6 |
| Q7 | **Server Python version** (core needs ≥3.11, hub ≥3.12; proposal 3.12) and an offline wheel route | Packaging | M1 |
| Q8 | **What is "official PnL"?** Is the TradeLevel IFRS figure the official one, on a CBR basis, including funding? | Reconciliation semantics | M5 |
| Q9 | **Frontend framework:** React + TypeScript (recommended) or stay vanilla | Frontend stack for the first migrated screen | M1 |

**Financial decisions on the critical path** (from `docs/program/DECISIONS.md`):
- DEC01: funding base and trade timing (M2);
- DEC02: accrual bases and calendars (M2);
- DEC03: REPO cash-leg funding (M2, if the book has REPO);
- DEC04: authoritative market cuts (M2);
- DEC06: OCP transfer convention (M2);
- DEC05: PnL attribution (M5);
- DEC08: numerical conventions, DV01 interpretation (M1–M3);
- DEC12: break thresholds (M5).
