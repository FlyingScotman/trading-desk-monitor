# Trading Desk Monitor execution pack

> **Superseded in part (16 October 2026).** Per [ADR-001](../architecture/adr/ADR-001-hosting-topology.md), the backend and frontend run on a **Windows server inside the work network**. The hub may be split between a Linux VPS (public collection) and the inside server, linked by immutable snapshots. The Hermes runtime agent, the work-PC-only backend and the bulk-download parts of this pack no longer apply. The current architecture is in [docs/architecture/](../architecture/README.md). `DECISIONS.md` DEC01–DEC12 remain valid.


This pack specifies how to build a public market and reference data service on a new VPS, reuse and extend the existing financial Python library, and deliver a local Windows calculation backend for Trading Desk Monitor. Work positions, private deal terms, funding rules, cash balances, PnL, and portfolio results stay inside the organization.

Prepared 14 October 2026, Europe/Moscow. Plan revision 0.1. The reviewed monitor baseline is commit a6ba30bf5573ada869a9594c3e69f73faf741ed2 on claude/clever-ramanujan-s4k3ed, data contract 1.3. The financial library and current Hermes collectors still require export and audit.

Read in this order:

1. EXECUTION_SPEC.md for architecture, scope, delivery steps, and release gates.
2. BASELINE_REVIEW.md for what the current monitor already supplies and what needs reconciliation.
3. DECISIONS.md for proposed defaults and financial decisions requiring the desk owner.
4. requirements.csv for the coverage checklist used by the library auditor and implementing agents.
5. backlog.json for task ownership, dependencies, deliverables, and acceptance criteria.
6. AGENT_HANDOFFS.md for prompts to give the existing Hermes agent, coding agents, reviewer, and deployment operator.
7. STATUS.md for the initial progress snapshot.

The first action is the existing VPS export prompt in AGENT_HANDOFFS.md. It prepares the library and collector code without exporting Hermes memory, credentials, or operational account files. The next coding task is an evidence based library audit, not an unbounded rewrite.

The canonical home is now docs/program/ in the existing private FlyingScotman/trading_desk_monitor repository. The local desk_monitor_backend folder is a linked Git worktree that displays only these project documents. See GITHUB_SETUP.md for repository ownership and local authentication. Keep backlog.json authoritative for task state; generate STATUS.md from it. GitHub issues may mirror task IDs and link to PRs, but must not become a second contradictory specification.

No VPS has been purchased or changed, no service deployed, and no financial library audited as part of this planning pack. Current implementation progress is recorded honestly in STATUS.md.
