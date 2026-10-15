# Trading Desk Monitor project status

As of 15 October 2026. Generated from backlog.json revision 0.2.

Scoped implementation is in progress: SCEN-01 now has a local fixed-RUB portfolio worker and a tested market-data-store response bridge. Other task states below remain from the initial planning pack and have not been comprehensively reconciled with intervening deliveries.

| Item | State |
|---|---|
| Monitor baseline | Source reviewed at a6ba30bf5573ada869a9594c3e69f73faf741ed2; contract 1.3 |
| Financial library and old collectors | On the existing Hermes VPS; clean export not supplied yet |
| New VPS and domain | Not selected or purchased for this project |
| Data providers and rights | Source Registry to be completed |
| Public data API and runtime Hermes | Not deployed |
| Local Windows calculation backend | Planned; only the existing repository scaffold was inspected |
| Tests | Planning structure validated; repository and library runtime tests not run for this pack |
| Tasks | 26 todo; 1 in progress; 0 in review; 0 done |
| Requirements | 61 tracked: 23 datasets, 26 calculation groups, 12 system requirements |

First ready tasks are CTRL-01 and EXPORT-01. The owner can give the existing VPS agent the first prompt in AGENT_HANDOFFS.md while the coordinator establishes the canonical repository files. The library audit starts after the export and control baseline exist.

Do not wait for all decisions to start independent work: source/API schema design and host planning can proceed while the library is being audited. Do not implement financial assumptions as final rules without the relevant decision.

The next owner inputs are the clean source export, provider names and access capabilities, host/domain/budget details, and initial supported instrument classes. Credentials use the operational secret channel. Work files and real calculation results stay at work.

This file is a summary. Change task state and evidence in backlog.json, then regenerate the summary. Do not maintain competing status in agent memory or separate chat histories.

## 15 October implementation evidence

See `../../../rub-fixed-income-core/implementation/risk-worker/HANDOFF.md`: core 0.6.0 is implemented locally;
436 tests pass on fresh frozen Python 3.11, with separate synthetic service-bridge
and fixing characterization evidence. SCEN-01 remains in progress: no monitor
wiring, queue/cancellation, carry/funding or Windows verification is claimed.
The monitor repository was inaccessible through the available GitHub account.

Floater findings are added to CORE-03, DATA-01 and HIST-01 acceptance: preserve
working realized-plus-projected service behavior, unify the low-level API, prevent
future-known fixing use, and reject missing required RUONIA observations.
