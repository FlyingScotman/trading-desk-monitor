# Roadmap

Revision 3, 19 October 2026. This is a sequencing view. Each repository keeps its own detailed task state in its own `STATUS.md`. `docs/program/backlog.json` is historical and is not reconciled with this file.

**What changed in revision 3.** Slices S1–S6 are built on public data and a synthetic book. The next stage connects the monitor to the desk's real data: positions, OCP and cash, official PnL, internal market data and, later, intraday trades. Firm-specific parsing goes to the private repository `trades-data-adapter` ([ADR-008](adr/ADR-008-private-adapter-boundary.md)); this repository stays generic. Q1 (where positions come from) is now answered for the daily book (§4).

**Two tracks.** The S slices are the backend and frontend track: each ends in something the owner can open. The adapter has its own A phases in its own repository. The tracks run in parallel and are coupled only through [08 Private adapter interface](08_PRIVATE_ADAPTER_INTERFACE.md): the SPI, the canonical datasets of contract 1.4, the reference adapter and the conformance kit. They meet at integration points I1–I6 (08 §11); a slice that needs real data closes at its integration point, and until then it runs on the reference adapter and synthetic canonical files.

## 0 First: real risks on the owner's books

The owner asked (19 October 2026) to put the positions loader first: six slices are built, and none has run on the desk's own book yet. So the next stage is the shortest path to the Desk Monitor showing PV, DV01 by bucket, CS01 and coverage for the real books at work. Everything else waits for it.

| Step | Who | What | Done when |
|---|---|---|---|
| F1 Hub token | owner (hub session) | `/v1/snapshots` is deployed (it answers 401, not 404, as of 19 October). Issue a consumer token with the `snapshots` scope for the desk backend | `desk hub-check` and `desk sync --last 5` work from home |
| F2 Backend on the work PC | backend | Windows install of the backend (not only the `tdm` prototype): an offline bundle for Python 3.11 win_amd64 with core pinned and the built frontend, `install_backend.bat`, `run_backend.bat`, embedded Postgres (`pgserver`, its Windows wheel checked first). Proxy: `HTTPS_PROXY` and the system certificate store; if the proxy blocks the hub (Q4), snapshots go through the drop folder (`desk import-drop`) | `desk config`, `desk db-migrate`, `desk eod` on the synthetic book run at work |
| F3 Positions through the inbox | backend, S7a | The minimum of [08](08_PRIVATE_ADAPTER_INTERFACE.md) for positions: loader and runner, reference adapter, `desk inbox scan/status`, ledger and revisions for `positions` and `books`, the conformance kit, `desk eod` from inbox positions | Synthetic canonical files reach a run; a test adapter loaded from a path passes the kit |
| F4 Positions adapter | adapter, A0 + A2 | Package by SPI v1. The positions report mapped from **the field list the owner gives**; the full profiler (A1) is not on this path | The kit passes on synthetic files of that layout |
| F5 First real run | owner at work | `desk adapter check --dir <positions>` (counts and codes only), then `desk inbox scan`, `desk eod`, `desk serve` | The Desk Monitor shows the real books; unmapped bonds are a known, small list |

F2, F3 and F4 run in parallel. After F5 the order below resumes, starting with the YTD backfill and the official PnL (S8).

## 1 The desk's day

The backend is driven by data arriving, not by a clock. Business date **D**:

| When | What arrives | Backend | User sees |
|---|---|---|---|
| Morning D | Positions report as of 00:00 of D (= close of the previous business day) | Load a revision. The close of D−1 is now known, so the EOD run of D−1 is rerun on it and supersedes the evening run made on the opening book | "Positions D received"; position diff vs the book the evening run used |
| Morning D (owner pulls it) | OCP and cash report | Load; compare reported OCP with OCP derived from positions | OCP and cash per book; OCP break if any |
| Morning D | Official PnL for D−1 (TradeLevel, T+1) | Load as an append-only revision; reconcile against our EOD D−1 on hub data and on internal data; run break analysis | Breaks for D−1 with causes and confidence; YTD ours vs official |
| During D | Hub intraday cuts; later trade exports every 1–2 h | Intraday run on the opening book (+ trades once they exist): PV, risk, flash PnL and explain | Intraday PnL with snapshot age |
| Evening D | Hub EOD snapshot (preliminary, then final) | EOD run D on the opening book (+ trades): flash PnL and explain | Flash PnL D |
| Evening / night D | Internal EOD market data | EOD-internal run D: the same book on the internal overlay; diff vs the hub run | Notification; market-data diff per instrument and curve |

Without a trade feed the evening run values the opening book; new trades of D show up the next morning as a position change. That gap is a named bucket ("new trades, no trade prices"), filled from the official PnL's trade lines in break analysis and closed for good by the trade feed.

**PnL explain on every snapshot.** `desk.pnl.flash` explains by stepwise exact repricing: a few full revaluations of ≤ 2k positions, seconds per run today (S4: 6–12 s per book run including the universe medians). So explain runs on every snapshot (intraday, EOD, internal), not only in the evening. If a book outgrows that, the intraday explain falls back to Taylor with a residual line.

## 2 Milestones

The order of work is set by [VERTICAL_SLICES.md](VERTICAL_SLICES.md). M1 and M3 are reached (S1–S6). M2 now runs on real positions.

| Milestone | Outcome | Slices | Status |
|---|---|---|---|
| **M1 Vertical slice** | Hub snapshot → core → desk store → desk API → screens on a synthetic book | S1–S5 | Done (live hub acceptance waits for hub H1–H4) |
| **M3 Interactive scenarios** | Taylor in the browser, exact reprice on the backend, first React screen | S6 | Done; T3 presets, historical scenarios, SSE open |
| **M2' Real book** | Folder ingestion, positions adapter, EOD runs on the real book (§0 first), then the YTD backfill | S7a, S7b, S8 | **Next: §0** |
| **M5 Official PnL and reconciliation** | Official PnL loaded; ours vs official per day, book, instrument; break analysis with causes and confidence; break register | S8, S10, S11 | |
| **M2'' Desk rules** | OCP, cash, funding per `ORG_CONVENTIONS.md` on real data | S9 | Needs DEC01, DEC03, DEC06 |
| **M8 Internal market data** | Internal EOD overlay, second EOD run, diff and notification | S10 | Needs the export format (owner, adapter A5) |
| **M4 Intraday** | Intraday runs with explain on every snapshot; SSE; later trades every 1–2 h | S12, S13 | S13 waits for the trade export |
| **M6 Coverage waves** | Derivatives and the hybrid book from term files | S14 | |
| **M7 Operate** | Alerting, backups, runbooks, remaining screens migrated, `window.DESK` removed | — | |

## 3 Work by repository

### trades-data-adapter (private)

Its own `ROADMAP.md` holds the detail. Phases, in the order the backend needs them:

| Phase | Outcome | Integration point (08 §11) |
|---|---|---|
| A0 | Repository, rules (no real data in Git), SPI v1 skeleton loadable from a path (`DESK_PRIVATE_ADAPTER`) | I1 |
| A1 | Metadata profiler v2 over all report families at once; the owner runs it at work and returns `metadata.zip` | — |
| A2 | Positions adapter (bonds and FX), security-id and book mapping | I2 (S7a) |
| A3 | Official PnL adapter (TradeLevel), reusing pnl_tool's hardened reader | I3 (S8) |
| A4 | OCP and cash adapter | I4 (S9) |
| A5 | Internal market data: agree an export format with the owner, then its adapter | I5 (S10) |
| A6 | Trades adapter, once the hourly export exists | I6 (S13) |

### trading_desk_monitor: backend

1. **Private input contract v1** (`contract/private/v1/`, [08](08_PRIVATE_ADAPTER_INTERFACE.md) §10): canonical `positions`, `cash_balances`, `ocp`, `pnl_official` (components: accrual, price, FX, fees, reserves; trade and position keys; revision), `trades`, `internal_market`. Plus the items already planned for 1.4: sparse Hessian, coverage and provenance states, lineage fields.
2. **`desk.inbox`** per [08](08_PRIVATE_ADAPTER_INTERFACE.md): adapter loader from `DESK_PRIVATE_ADAPTER`, folder watcher (polling; a file counts once its size and time are stable), SHA-256 ingest ledger and archive, reference adapter, conformance kit `desk adapter check`, events table; `desk inbox scan|status|replay`.
3. **Store**: bitemporal tables for the new datasets (positions exist since S5); events; break register.
4. **Orchestrator: the day cycle** (§1) as a dependency graph, not a cron list: an arrival triggers the runs that depend on it, and a rerun supersedes. `desk backfill --from 2026-01-01`: EOD runs and flash PnL for every date with positions, then reconciliation for every date with official PnL.
5. **`desk.pnl.reconcile`**: ours vs official per date × book × instrument, matched on trade or position keys. Break analysis as a substitution waterfall: swap in internal market data (Δ market data), official positions (Δ positions), CBR FX (Δ FX basis), cash flows and accrual convention (Δ carry), new-trade lines (Δ new trades); the remainder is unexplained. Each cause gets a confidence: *confirmed* (the swap closes the break within the DEC12 threshold), *likely*, *unexplained*. Component matching: our carry vs official accrual, our price effect vs price change, FX vs FX.
6. **`desk.marketdata.internal`**: the internal overlay replaces hub inputs it covers (curve points, bond prices, FX), keeps the rest, and records which. The EOD-internal run is a separate run type with its own lineage.
7. **`desk_rules`**: OCP, cash, funding, dual FX basis from `ORG_CONVENTIONS.md` (S9).
8. **Explain** on every snapshot (§1); intraday runs; SSE (S12).
9. Carried from S4–S6: interface catalogue (`coverage`, `totals`), curve-spec mapping table, T3 presets, historical scenarios, custom scenarios, job cancel, worker out of the API process.

### trading_desk_monitor: frontend

Real-data screens come first; the remaining prototype screens migrate after them.

1. **Day status**: per date, what has arrived (positions, OCP and cash, hub EOD, internal data, official PnL), which runs exist and which superseded which; the event feed and notifications.
2. **PnL vs official**: YTD cumulative ours vs official by book, daily bars, the gap line, drill-down to instrument.
3. **Reconciliation**: the break waterfall per date and book, causes with confidence, instrument drill-down; the user sets a break's status (ours, finance, market data, timing, accepted) and a comment, kept in the break register.
4. **Market data diff**: hub vs internal per curve point, bond price and FX rate, with the PnL impact of each difference.
5. **PnL explain**: per snapshot, carry / rates / spread / FX / new trades / residual (from the prototype Scenario & PnL Lab).
6. Desk Monitor in React with OCP and cash; then Pivot Studio, Widget Board, Bonds ([05](05_FRONTEND_INTEGRATION.md) §4).
7. Live updates over SSE; run badge with age and coverage everywhere.

### market-data-server (hub)

The list with checks is [briefs/HUB.md](briefs/HUB.md). On the critical path now:
- H1–H4: the snapshot layer deployed, with a consumer token (S3 live acceptance);
- **H26: EOD snapshots back to 2026-01-01**, needed by the YTD backfill (S8). Until then the backfill runs on the dev export (`tools/devdata/`);
- intraday cuts for S12.

Bank-internal sources are not hub work: they enter through the private adapter.

### rub-fixed-income-core

Plan: `rub-fixed-income-core/docs/PRODUCT_ROADMAP.md`. Desk items: [briefs/CORE.md](briefs/CORE.md) (C1 offer windows, C2 valuation at trade date, C3 release tags, C4 system context, C5 horizon explain, C6 corporate accrual basis). Reconciliation may add accrual-convention items once breaks are classified.

### pnl_tool

Stays local and unchanged. Its XLSX reader, SHA-256 idempotent import, snapshot-revision logic and "no silent zeros" rules are copied into the private adapter (A3, owner's consent 19 October 2026), not imported.

## 4 Owner answers and open questions

| ID | Question | Status | Consequence |
|---|---|---|---|
| Q1 | Where do positions and trades come from? | **Answered (19 October 2026)** for the daily book: a morning positions report and a separate OCP and cash report, dropped into folders. Intraday trade exports (every 1–2 h) will come later | Folder ingestion through the private adapter (ADR-008); without trades, intraday reprices the opening book |
| Q2, Q2a | Hosting | Answered: Windows inside; one hub on the VPS | [ADR-001](adr/ADR-001-hosting-topology.md) |
| Q3, Q6–Q9 | Licensing, products, Python, official PnL, frontend | Answered (16 October 2026) | Unchanged; official PnL = TradeLevel IFRS on the CBR basis, including funding |
| Q4 | Proxy facts | **Open** | Blocks the snapshot pull from inside; the drop folder is the fallback |
| Q10 | Which date does the morning positions report describe? | **Answered (19 October 2026):** positions as of 00:00 of D, so the close of the previous business day (`time_basis = start_of_day`, 08 §6.5). Whether positions opened and closed within a day appear is unknown | The morning file reruns the EOD of D−1. Intraday round trips: the profiler (A1) looks for zero-quantity rows; otherwise they reach us only through official PnL or the trade feed |
| Q11 | Format of the internal EOD market data | **Open**; the adapter repository proposes a template (A5) | S10 |
| Q12 | Folders, file names and how far back they go | **Moved to the adapter repository** (A1, the profiler collects it) | Backfill range (S8) |
| Q13 | Can pnl_tool's reader code be copied into the private adapter repository? | **Answered: yes** (19 October 2026) | A3 copies it |

**Financial decisions on the critical path** (`docs/program/DECISIONS.md`): DEC01 funding base, DEC03 REPO cash leg, DEC06 OCP transfers (S9); DEC05 PnL attribution, DEC11 restatements, DEC12 break thresholds (S8, S11); DEC02, DEC04, DEC08 as before.
