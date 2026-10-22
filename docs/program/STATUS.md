# Trading Desk Monitor project status

As of 22 October 2026. Generated from backlog.json revision 0.3.

Task states were reconciled with the code of the four repositories on 22 October 2026 (monitor, `market-data-hub`, `rub-fixed-income-core`, `trades-data-adapter`). The backlog is the original planning pack: it records which of its tasks are done, but it is **not** the live plan. The live sequence is [ROADMAP.md](../architecture/ROADMAP.md) §0; the owner's requests and their states are in [OWNER_REQUESTS.md](OWNER_REQUESTS.md); the process is in [ORCHESTRATION_PLAN.md](ORCHESTRATION_PLAN.md).

| Item | State |
|---|---|
| Monitor baseline | Source reviewed at a6ba30bf5573ada869a9594c3e69f73faf741ed2 (contract 1.3); the backend (`backend/src/desk`) has replaced the prototype store since S5 |
| Financial library | `rub-fixed-income-core` 0.33.0, pinned by commit in `backend/pyproject.toml` |
| Market-data hub, VPS and domain | `market-data-hub` 0.2.0 in production on the VPS with HTTPS (checked 21 October 2026); 0.3.x and 0.4.0 in review, installed by the owner |
| Public data API | `/v1/snapshots` and `/v1/history/*` with scoped tokens |
| Local Windows calculation backend | Offline bundle installed at work (20 October 2026), updated by zip patches; real books run through the private adapter |
| Hermes runtime agent | Not planned (ADR-001); AGENT-01 and AGENT-02 are kept as obsolete, state `blocked` |
| Tests | See ORCHESTRATION_PLAN §1: backend `pytest`, root `unittest`, frontend `npm test` |
| Tasks | 4 todo; 5 in progress; 1 in review; 16 done; 2 blocked (obsolete) |
| Requirements | 61 tracked: 23 datasets, 26 calculation groups, 12 system requirements (requirements.csv, not re-verified) |

Open tasks: DESK-03 (funding, FX, OCP, HTM and official PnL; nothing of it in the backend yet: funding in flash PnL is 0), UI-02 (currency × instrument type table), METHOD-01 (decisions, see DECISIONS.md), SCEN-01 (cancellation, historical scenarios, T3 presets), UI-01, OPS-01 (off-host hub backups, rollback rehearsal), PILOT-01 (needs official PnL), QA-01 (owner acceptance), RELEASE-01 and ACCEPT-01.

Derivatives valued by core in the monitor are one canonical item, ROADMAP §2a (M6); CORE-03 is done on the core side only.

Do not implement financial assumptions as final rules without the relevant decision ([DECISIONS.md](DECISIONS.md)). Work files and real calculation results stay at work.

This file is a summary. Change task state and evidence in backlog.json, then regenerate the summary.

## History

15 October 2026: core 0.6.0 was implemented locally (436 tests on fresh frozen Python 3.11); SCEN-01 had started; the monitor repository was not yet reachable through the available GitHub account. Floater findings were added to CORE-03, DATA-01 and HIST-01 acceptance (realized-plus-projected behaviour, unified low-level API, no future-known fixings, rejection of missing RUONIA observations); they are covered by core 0.8, 0.26 and 0.33.
