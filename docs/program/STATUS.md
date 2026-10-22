# Trading Desk Monitor project status

As of 22 October 2026. Generated from backlog.json revision 0.3.

Task states were reconciled with the code of the four repositories on 22 October 2026 (monitor, `market-data-hub`, `rub-fixed-income-core`, `trades-data-adapter`). The backlog is the original planning pack: it records which of its tasks are done, but it is **not** the live plan. The live sequence is [ROADMAP.md](../architecture/ROADMAP.md) §0; the owner's requests and their states are in [OWNER_REQUESTS.md](OWNER_REQUESTS.md); the process is in [ORCHESTRATION_PLAN.md](ORCHESTRATION_PLAN.md).

| Item | State |
|---|---|
| Monitor baseline | Source reviewed at a6ba30bf5573ada869a9594c3e69f73faf741ed2 (contract 1.3); the backend (`backend/src/desk`) has replaced the prototype store since S5 |
| Financial library | `rub-fixed-income-core` **0.35.0** (commit `f3cbabc`), pinned by commit in `backend/pyproject.toml`: CNY bonds on the XCCY curve and USD / EUR bonds on the par-yield proxies behind `proxy_curves_for_risk` (core BACKLOG #124), FX forwards, NDFs, swaps and crosses from `trade_terms` through `value_portfolio` v3 (`valuation/derivatives.py`; inputs `curves/fx.py` from hub contract 1.4). Provisional choices: [DECISIONS](DECISIONS.md) OQ-05…OQ-16 |
| Market-data hub, VPS and domain | `market-data-hub` 0.2.0 in production on the VPS with HTTPS (checked 21 October 2026); 0.3.x and 0.4.0 in review, installed by the owner |
| Public data API | `/v1/snapshots` and `/v1/history/*` with scoped tokens |
| Local Windows calculation backend | Offline bundle installed at work (20 October 2026), updated by zip patches; real books run through the private adapter |
| Hermes runtime agent | Not planned (ADR-001); AGENT-01 and AGENT-02 are kept as obsolete, state `blocked` |
| Tests | See ORCHESTRATION_PLAN §1: backend `pytest`, root `unittest`, frontend `npm test` |
| Wave A (ROADMAP §0.2) | **Done** and merged on 22 October: P1 contract and SPI (private 1.2), P2 funding engine (`desk/rules/`, not wired into runs), P4 currency × instrument type table (UI-02, in review), P9 documents. Open: P3, P5 (core 0.34.0 pickup), P6 (hub), P7 (core), P8 (adapter A7) |
| Tasks | 2 todo; 6 in progress; 2 in review; 16 done; 2 blocked (obsolete) |
| Requirements | 61 tracked: 23 datasets, 26 calculation groups, 12 system requirements (requirements.csv, not re-verified) |

Open tasks: DESK-03 (funding, FX, OCP, HTM and official PnL; the contract and the funding engine exist since wave A, but funding in flash PnL is 0 until wave B1), UI-02 (currency × instrument type table, in review: owner decisions on the currency of CCS / FX forwards / FX futures and on USD sovereigns), METHOD-01 (decisions, see DECISIONS.md), SCEN-01 (cancellation, historical scenarios, T3 presets), UI-01, OPS-01 (off-host hub backups, rollback rehearsal), PILOT-01 (needs official PnL), QA-01 (owner acceptance), RELEASE-01 and ACCEPT-01.

Derivatives valued by core in the monitor are one canonical item, ROADMAP §2a (M6); CORE-03 is done on the core side only.

Do not implement financial assumptions as final rules without the relevant decision ([DECISIONS.md](DECISIONS.md)). Work files and real calculation results stay at work.

This file is a summary. Change task state and evidence in backlog.json, then regenerate the summary.

## History

15 October 2026: core 0.6.0 was implemented locally (436 tests on fresh frozen Python 3.11); SCEN-01 had started; the monitor repository was not yet reachable through the available GitHub account. Floater findings were added to CORE-03, DATA-01 and HIST-01 acceptance (realized-plus-projected behaviour, unified low-level API, no future-known fixings, rejection of missing RUONIA observations); they are covered by core 0.8, 0.26 and 0.33.
