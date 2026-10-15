# Trading Desk Monitor baseline review

## Reviewed version

Repository: https://github.com/FlyingScotman/trading_desk_monitor

Branch: claude/clever-ramanujan-s4k3ed

Commit: a6ba30bf5573ada869a9594c3e69f73faf741ed2

Commit subject: Upgrade to data contract 1.3 funding by position dual FX basis HTM.

Contract 1.3 and organization conventions supersede the earlier 1.2 assessment in the conversation. This is a source review; the repository tests have not been executed for this pack. The library and existing VPS collectors are not yet available for inspection.

## What the baseline already provides

The four screens remain Desk Monitor, Widget Board, Pivot Studio, and Scenario and PnL Lab. The Python tdm package stores input datasets and calculation runs in SQLite, validates and publishes runs, exposes read APIs, and assembles the window.DESK object consumed by the existing screens.

Contract 1.3 adds book_type TRADING / HTM / DERIVS and legal entity; position funding POS / HTM and book CASH / REPO funding; market and effective CBR FX series including CHF; ofz_spr01 and RUB RUONIA based bond risk; actual OCP transfer inputs; T+1 official PnL attachment; HTM treatment; carry and funding scenarios. There are 23 input datasets. VaR and limits remain explicitly outside scope.

The SDK already derives funding, resolves funding rules, calculates some date and FX derivatives, attaches official PnL, and stores scenario results. Reuse or extract these functions after reconciling their conventions. A new backend should not duplicate them silently.

Interactive shock and carry approximations already execute in prototypes/desk-util.js. Full revaluation result storage exists. A local asynchronous worker for arbitrary full revaluation is only designed, not implemented. The calculation service example still yields demo positions.

Sources:
- [Contract 1.3](https://github.com/FlyingScotman/trading_desk_monitor/blob/a6ba30bf5573ada869a9594c3e69f73faf741ed2/docs/DATA_CONTRACT.md#L39)
- [Organization conventions](https://github.com/FlyingScotman/trading_desk_monitor/blob/a6ba30bf5573ada869a9594c3e69f73faf741ed2/docs/ORG_CONVENTIONS.md)
- [Data requirements](https://github.com/FlyingScotman/trading_desk_monitor/blob/a6ba30bf5573ada869a9594c3e69f73faf741ed2/docs/DATA_REQUIREMENTS.md)
- [Scenario design](https://github.com/FlyingScotman/trading_desk_monitor/blob/a6ba30bf5573ada869a9594c3e69f73faf741ed2/docs/SCENARIOS.md)
- [Calculation stub](https://github.com/FlyingScotman/trading_desk_monitor/blob/a6ba30bf5573ada869a9594c3e69f73faf741ed2/examples/calc_service_stub.py)

## Reconciliation items before operational use

| ID | Observation and consequence | Required response |
|---|---|---|
| B01 | Confirmed organization rules and contract section 0.4 use the previous working day's clean MV for funding accrual. The documented implementation and derive_funding use current run MV and current cash snapshots. | Decide period base and new trade treatment in DEC01. Reconcile backend and browser tests. Keep any legacy approximation explicitly named. |
| B02 | Funding rules and instrument terms are current versions. Historical replay can therefore apply today's funding spread or offer date to a past date. | Version terms and rules by effective date and knowledge date; archive the selected versions in each local calculation input bundle. |
| B03 | Official PnL import permits book totals, while fill_official_pnl and attach_official_pnl update per-position totals. Optional official components are loaded but not reconciled by those functions. | Model position and book official coverage separately; do not invent position allocations or count funding twice. Record revisions on T+1 and subsequent corrections. |
| B04 | Market FX in organization conventions is an internal input around 19:00. An external feed is a proposed supplier, not automatically an equivalent mark. | Define the authoritative cut and local override precedence in DEC04. Keep both provider and internal mark identities. |
| B05 | Full scenario results refer to mutable global scenario IDs. Browser storedResult substitutes zero for missing position results. | Bind results to a definition hash, base run, market snapshot, model version, and explicit coverage. Incomplete results must not be labelled full portfolio revaluation. |
| B06 | Server scenario cache uses scenario row count. Replacing results with the same row count need not change its cache key. | Introduce explicit result revisions and invalidate by revision. |
| B07 | Historical scenario construction requires some curves at endpoints, but does not enforce all required factors or a continuous business-day window. Missing factors can contribute zero. | Enforce factor coverage and true calendar horizons; retain reasons for excluded windows. |
| B08 | fx_cbr_next selects the first available future effective date. With a historical database this can select a distant or not-yet-known observation. | Require the expected effective date and publication availability at the snapshot cut. Test weekends and missing records. |
| B09 | Sparse tridiagonal gamma and a common 500 bp approximation threshold are documented from a simplified representation and sample tests. | Audit against actual curve construction and each product. Do not truncate a bootstrapped curve Hessian by assumption or interpret that threshold as a general accuracy guarantee. |
| B10 | OTC summary rows omit detailed leg schedules and conventions; inflation and extension risk are proposals without the complete v1.3 factor contract. | Add a versioned private trade payload or deterministic templates. Explicitly defer or design proposed special factors through a contract change. |
| B11 | Missing PnL and some risks are defaulted to zero; official history can fall back to an estimate. The demo fallback can populate screens when no published run exists. | Add coverage and provenance states. Operational mode must show missing, pending, stale, failed, and estimated separately from measured zero and official. |
| B12 | The supplied server listens on all interfaces and does not implement end-user authentication. | Use loopback on the work PC. Authentication and TLS on the new external service are separate production requirements. Audit all four views for identical aggregation and filtering rules. |

These are source based findings to reproduce during implementation, not claims of a failed runtime test.

Code references:
- [Funding implementation](https://github.com/FlyingScotman/trading_desk_monitor/blob/a6ba30bf5573ada869a9594c3e69f73faf741ed2/tdm/writer.py#L350)
- [Official attachment](https://github.com/FlyingScotman/trading_desk_monitor/blob/a6ba30bf5573ada869a9594c3e69f73faf741ed2/tdm/writer.py#L758)
- [Stored scenario selection](https://github.com/FlyingScotman/trading_desk_monitor/blob/a6ba30bf5573ada869a9594c3e69f73faf741ed2/prototypes/desk-util.js#L165)
- [Server cache](https://github.com/FlyingScotman/trading_desk_monitor/blob/a6ba30bf5573ada869a9594c3e69f73faf741ed2/tdm/server.py#L17)
- [CBR next rate](https://github.com/FlyingScotman/trading_desk_monitor/blob/a6ba30bf5573ada869a9594c3e69f73faf741ed2/tdm/market.py#L99)
- [Historical windows](https://github.com/FlyingScotman/trading_desk_monitor/blob/a6ba30bf5573ada869a9594c3e69f73faf741ed2/tdm/histscen.py#L128)

## Source precedence

Use confirmed rules in ORG_CONVENTIONS.md, then the detailed DATA_CONTRACT.md sections, then DATA_REQUIREMENTS.md and SCENARIOS.md. Code and fixtures describe existing behavior and must be reconciled with the governing conventions. An explicit TBD, assumption, or proposal remains undecided even when the prototype implements a default. Resolve contradictions through DECISIONS.md before releasing the affected capability.
