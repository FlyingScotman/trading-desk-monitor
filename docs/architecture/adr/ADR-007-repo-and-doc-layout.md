# ADR-007 Repository layout and documentation ownership

Status: proposed (16 October 2026).

## Context

Current state:
- **trading_desk_monitor:** documents (the program pack), contract 1.3, the `tdm` prototype store and the UI mockups.
- **market-data-server** and **rub-fixed-income-core:** separate repositories under a different GitHub account.
- **pnl_tool:** standalone and not under git.
- **desk_monitor_backend:** a linked worktree of trading_desk_monitor that showed only the program documents.

The owner sees the backend as the heart of the project.

## Decision

1. **`trading_desk_monitor` is the system repository**, a monorepo for the desk application:
   - `backend/` (new: orchestrator, adapters, valuation, desk rules, store, API, worker);
   - `frontend/` (new; `prototypes/` stays as the visual spec until replaced);
   - `contract/` (desk contracts: bundle schema, CSV templates, later the desk OpenAPI);
   - `docs/architecture/` (system docs and ADRs);
   - `docs/` (desk specifications);
   - `docs/program/` (decision register and history).

   `tdm/` stays until `backend/` replaces it.
2. **Producers own their outgoing contracts:**
   - the hub owns the Market Snapshot Contract;
   - core owns its domain model and API docs;
   - the backend owns the desk API and run bundle.

   The system repo keeps the **interface catalogue** ([README](../README.md)) with the pinned version of each.
3. **Component internals stay in the component repos.** The hub keeps `DESIGN.md` and `STATUS.md`; core keeps `ARCHITECTURE.md`, `CONVENTIONS.md` and its numerical docs. They link to this folder for system context instead of repeating it.
4. Hub and core stay separate repositories: different release cycles, and core is reusable. The backend pins exact released versions of core, and a schema version of the hub contract.
5. **Hosting:** the system repository is mirrored to `FiveLimaGolf/trading-desk-monitor`, alongside `FiveLimaGolf/rub-fixed-income-core` and `FiveLimaGolf/market-data-hub`. Which account becomes canonical is open question Q5 in [ROADMAP](../ROADMAP.md).
6. The `desk_monitor_backend` worktree has served its purpose (its branch is merged) and can be removed.

## Consequences

- One place to read the whole system. Contracts have a single owner each, and no repo needs another repo checked out for CI.
- Status lives with the code. Each repository keeps its own `STATUS.md`; the system `ROADMAP.md` links to them rather than duplicating task state.
