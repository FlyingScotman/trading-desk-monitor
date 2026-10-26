# Roadmap

Revision 4, 22 October 2026 (revision 3: 19 October). This is a sequencing view. Each repository keeps its own detailed task state in its own `STATUS.md` or roadmap. `docs/program/backlog.json` is the original planning pack: its task states were reconciled with the code on 22 October 2026, but the sequence below is the live plan. Decisions: [DECISIONS.md](../program/DECISIONS.md) (canonical for DEC01–DEC12 and the open questions OQ-NN).

**What changed in revision 4.** The real books run at work since 20 October (the old §0, steps F1–F5, is done). §0 is now the next sequence: the firm-specific work goes one topic at a time with the owner (funding, derivatives risk, OCP, cash, then official PnL and reconciliation), and the generic work runs in parallel in two waves. M2'' is unblocked on the decision side (DEC03 is answered). The adapter phases A7–A10 are added (§3). The "derivatives valued by core" item, which appeared under five names, is one canonical item (§2a).

**What changed in revision 3.** Slices S1–S6 are built on public data and a synthetic book. The next stage connects the monitor to the desk's real data: positions, OCP and cash, official PnL, internal market data and, later, intraday trades. Firm-specific parsing goes to the private repository `trades-data-adapter` ([ADR-008](adr/ADR-008-private-adapter-boundary.md)); this repository stays generic. Q1 (where positions come from) is now answered for the daily book (§4).

**Two tracks.** The S slices are the backend and frontend track: each ends in something the owner can open. The adapter has its own A phases in its own repository. The tracks run in parallel and are coupled only through [08 Private adapter interface](08_PRIVATE_ADAPTER_INTERFACE.md): the SPI, the canonical datasets of contract 1.4, the reference adapter and the conformance kit. They meet at integration points I1–I6 (08 §11); a slice that needs real data closes at its integration point, and until then it runs on the reference adapter and synthetic canonical files.

## 0 Next: the desk's own rules and derivatives (from 22 October 2026)

**Done: real risks on the owner's books.** The previous §0 (steps F1–F5, 19 October: hub token, backend on the work PC, positions through the inbox, positions adapter, first real run) is complete: the Desk Monitor and the monitors show the real books at work since 20 October 2026 ([OWNER_REQUESTS](../program/OWNER_REQUESTS.md) R1, R22, R27). What the books still lack is everything the organization's rules add on top of market risk: funding, the risk of derivatives priced by their terms, OCP, cash and the official PnL.

### 0.1 Firm-specific: one topic at a time, with the owner

Each step follows the adapter's process (`trades-data-adapter` `CLAUDE.md`: fields → metadata extractor → metadata → adapter on synthetic files → `desk adapter check` at work). What the owner supplies for each step, and the organization's own answers, are kept in the private adapter (its `ROADMAP.md`, `docs/ORG_RULES.md`), not here. In parallel from day one (minutes of the owner's time): start saving the daily cash-balance report and, if one exists, a derivative-terms export, because history accumulates only from the day saving starts.

| Step | Topic | Generic prerequisites (§0.2) | Closes | Decisions and open questions |
|---|---|---|---|---|
| 1 | **Funding** of the books: entities, book funding modes, rate table with `valid_from`, the funding engine on real rules; HTM and REPO as their exports arrive | P1 (contract, SPI `entities`/`funding_rules`/`settings_tabs`), P2 (engine), P8 (adapter A7); then B1 | M2'' (funding part), backlog DESK-03 (funding) | DEC01, DEC02, DEC03 residual (term threshold), OQ-01, OQ-02 |
| 2 | **Derivatives risk**: swaps (IRS/OIS, key-rate), futures (FX, OFZ, RUONIA and key-rate) and FX forwards, **including their interest-rate risk** (today an FX forward carries FX delta only and a swap's reported DV01 sits in one bucket); the meaning of the report's NPV, DV01 and CS01; product classification; trade terms (A9) | P3 (derivatives in core, §2a), P6 (hub inputs H11, H13), P7 (core FX items) | M6 (§2a) | DEC08 |
| 3 | **OCP** (ОВП): reported OCP vs OCP derived from positions, by component (cash and to-be-settled, PV of currency legs, futures, residual); OCP transfers | P1 (`ocp` schema); B2 | M2'' (OCP part), backlog DESK-03 (OCP) | DEC06, DEC12, OQ-04 |
| 4 | **Cash**: cash balances per book with history; funding of cash-funded books and other entities; cash breaks between systems | P1 (`cash_balances` schema, multi-date history); B3 | M2'' (cash part); completes step 1 | DEC01 |
| then | **Official PnL and reconciliation**: the official PnL report → `pnl_official` (adapter A3), YTD ours vs official, break analysis and register | P1 (`pnl_official` schema); B4 | M5 (S8 part 2, S11) | DEC05, DEC11, DEC12 |

The official PnL may move earlier if the owner can run the official PnL report's extractor sooner: the report may also give the PV of currency legs (step 3) and a funding figure to accept step 1 against.

### 0.2 Generic: wave A in parallel now, wave B after it

No firm-specific data is needed. File zones do not overlap within a wave; the shared run files (`api/compat.py`, `publish/tdm_run.py`, `pnl/flash.py`) are left to wave B, except `tdm_run.direct_row` for P3.

| Package | What | Main zone |
|---|---|---|
| **P1 Contract and SPI** | Private contract schemas for `ocp`, `cash_balances`, `pnl_official` (drafts for `trades`, `internal_market`, `trade_terms`, `price_policy`); a funding mode for futures books if OQ-01 says so (private 1.2); SPI extension `describe()["settings_tabs"]`, `reference()["entities"]`, `reference()["funding_rules"]`; storage and revisions of the new datasets; [08](08_PRIVATE_ADAPTER_INTERFACE.md) updated (integration points for A7, A9, A10); conformance kit | `backend/src/desk/inbox/`, `contract/private/v1/`, `store`, 08 |
| **P2 Funding engine module** | `backend/src/desk/rules/funding.py`: modes per `book_type`, a rate per calendar day from fixings, simple interest, interest in RUB at the CBR rate, accrual bases as parameters, `valid_from`; parity with the prototype's `tdm/writer.py` `derive_funding` on demo rules; the currency funding liability for FX delta as a function (OQ-04), not wired in | new `backend/src/desk/rules/` |
| **P3 Derivatives in core** | The canonical item §2a: swaps, FX forwards and NDF, futures through core `value_portfolio` (result v3) next to the reported value, computed vs imported flag, comparison with the report's NPV | new `valuation/derivatives.py`, `valuation/positions.py`, `marketdata/adapter.py`, `orchestrator.py` |
| **P4 Currency × instrument type table** | Backlog UI-02 (§3 frontend item 8): instrument kind from the adapter's product group and the bond class, in `/aggregates` and in the monitors; plus small UI items (official PnL caption, default-bond yields, position card speed) | `api/` (dims, concepts), `prototypes/concepts/` |
| **P5 CNY and USD/EUR bonds in core** | R8 phase 2, bond part: CNY bonds on `cny_ruonia_xccy_v1` (drop the `not_rub` rejection for CNY), USD/EUR on proxy curves behind a flag; intraday accrued for known coupons | `valuation/bonds.py`, `fallback.py`, `curves/` |
| **P6 Hub inputs** | In `market-data-hub`: futures prices in the snapshot (H11), FX swaps / swap points with value dates (H13), RUONIA index and declared gaps (H12), CPI levels (H10), SARON, publication calendars (H17); snapshot contract 1.4; H10–H15 entered in the hub's own roadmap | `market-data-hub` |
| **P7 Core FX items** | In `rub-fixed-income-core`: #46 points-held FX risk, #49 NDF fixing and USD spot lag, #40 SOFR/€STR bootstraps, #103 basis scenarios, #113 key-rate/RUONIA basis swap, #102 derivatives in the local worker; release tags (C3) | `rub-fixed-income-core` |
| **P8 Adapter A7 on synthetic data** | In `trades-data-adapter`: reading and checking `entities.json`, `funding.json`, the book funding mode; `reference()` with `entities` and `funding_rules`; `describe()["settings_tabs"]`; synthetic samples only. Starts once P1 has fixed the SPI extension in 08 | `trades-data-adapter` |
| **P9 Documents** | This revision; backlog and STATUS reconciled; DECISIONS canonical for the generic IDs, with the open questions; ORCHESTRATION_PLAN | `docs/` |

**Wave A status (22 October 2026).** P1, P2, P4 and P9 are done and merged on `claude/blissful-ramanujan-ukia51`: private contract 1.2 with dataset storage (P1), the funding engine in `backend/src/desk/rules/` with `rules/reference.py` reading P1's `reference()` funding rules and entities (P2), the currency × instrument type table in the cockpit (P4, UI-02 in review), this revision (P9). P5 can start: core **0.34.0** (commit `15f63e3`) values CNY / USD / EUR bonds on their own currency's curve and awaits the monitor's pickup (core BACKLOG #132). Open: P3, P5, P6, P7, P8.

Wave B, after wave A:

| Package | What | Needs |
|---|---|---|
| B1 Funding in the run | `funding` and `fx_funding` in flash PnL, funding lines, `FUNDING_RULES` in compat, the currency liability in the FX delta of bond books (per OQ-04) | P1, P2, P8 |

**B1 status (22 October 2026): done, except the OQ-04 part.** `publish/funding_run.py` runs the engine in every
desk-store run (EOD and intraday; `desk_run.publish`): the adapter's `funding_rules`, `entities` and the new
private/1.3 `funding_policy` (HTM base, DEC01 timing, REPO policy, write-off; 08 §10), positions of the run and of the
previous default run, HTM lots from the report's purchase fields (`purchase_accrued_ccy`, 1.3), `cash_balances`,
the hub's fixings and CBR rates. Published: the run's `funding` block (`run_reference`), a position's
`fact.extra.funding` and PnL component `funding` (the pages show it as its own factor next to the carry, which is without it: TDM#168), book totals, run log lines and
problems, `/api/v1/runs/{id}/funding`, window.DESK `funding` / `fundingPnl` / `fundingPnlByBook` / `FUNDING_RULES` /
`fundingStatus`, the C01 cockpit's funding panel and the position card. Without rules: "not configured", never 0.
Not done, by decision: `fx_funding` and the funding liability in the FX delta stay 0 / absent until OQ-04; REPO deals
need `trade_terms` `repo` (a later minor). The private adapter (A7) must emit `funding_policy` and
`purchase_accrued_ccy`.
| B2 OCP | Loading `ocp`; OCP from positions vs from the report, by component; screen | P1, P2, P3 |
| B3 Cash | Loading `cash_balances`; funding of cash-funded books and other entities; cash-break screen | P1, P2, the cash export (step 4) |
| B4 Official PnL and reconciliation | S8 part 2 and S11: `pnl/official.py`, `pnl/reconcile.py`, API, screens | P1, adapter A3 |
| B5 Day status | S7b: `/api/v1/inbox`, `/api/v1/events`, screen | P1 |
| B6 Hub panel and OTC history | Hub WEB-7, WEB-13 | — |

**B3 status (25 October 2026, issue #61): built on synthetic data.** `desk.cash` compares the `cash_balances` in force
on a date per (entity, book, currency) across ledgers (`match`, `break`, `one_ledger`, `single`); `/api/v1/cash` and
the monitors' C15 show the balances, accounts, history and breaks. The funding run reads its cash through it: a
balance whose ledgers disagree is a problem and not funded (no ledger is authoritative in the contract: the
conservative default), agreeing ledgers fund the book. Acceptance on real data waits for the adapter's cash report
(A8) and DEC01.

## 1 The desk's day

The backend is driven by data arriving, not by a clock. Business date **D**:

| When | What arrives | Backend | User sees |
|---|---|---|---|
| Morning D | Positions report as of 00:00 of D (= close of the previous business day) | Load a revision. The close of D−1 is now known, so the EOD run of D−1 is rerun on it and supersedes the evening run made on the opening book | "Positions D received"; position diff vs the book the evening run used |
| Morning D (owner pulls it) | OCP and cash report | Load; compare reported OCP with OCP derived from positions | OCP and cash per book; OCP break if any |
| Morning D | Official PnL for D−1 (the official trade-level PnL report, T+1) | Load as an append-only revision; reconcile against our EOD D−1 on hub data and on internal data; run break analysis | Breaks for D−1 with causes and confidence; YTD ours vs official |
| During D | Hub intraday cuts; later trade exports every 1–2 h | Intraday run on the opening book (+ trades once they exist): PV, risk, flash PnL and explain | Intraday PnL with snapshot age |
| Evening D | Hub EOD snapshot (preliminary, then final) | EOD run D on the opening book (+ trades): flash PnL and explain | Flash PnL D |
| Evening / night D | Internal EOD market data | EOD-internal run D: the same book on the internal overlay; diff vs the hub run | Notification; market-data diff per instrument and curve |

Without a trade feed the evening run values the opening book; new trades of D show up the next morning as a position change. That gap is a named bucket ("new trades, no trade prices"), filled from the official PnL's trade lines in break analysis and closed for good by the trade feed.

**PnL explain on every snapshot.** `desk.pnl.flash` explains by stepwise exact repricing: a few full revaluations of ≤ 2k positions, seconds per run today (S4: 6–12 s per book run including the universe medians). So explain runs on every snapshot (intraday, EOD, internal), not only in the evening. If a book outgrows that, the intraday explain falls back to Taylor with a residual line.

## 2 Milestones

The order of work is set by [VERTICAL_SLICES.md](VERTICAL_SLICES.md) and, from 22 October, by §0. M1, M3 and M2' are reached; M4 is partly reached.

| Milestone | Outcome | Slices | Status |
|---|---|---|---|
| **M1 Vertical slice** | Hub snapshot → core → desk store → desk API → screens on a synthetic book | S1–S5 | Done; live hub acceptance done (hub 0.2.0 in production with snapshots, [E2E 21.10](../program/E2E_LIVE_2026-10-21.md)) |
| **M3 Interactive scenarios** | Taylor in the browser, exact reprice on the backend, first React screen | S6 | Done; T3 presets, historical scenarios, SSE open |
| **M2' Real book** | Folder ingestion, positions adapter, EOD runs on the real book (§0 first), then the YTD backfill | S7a, S7b, S8 | Done for S7a (real books since 20.10) and S8 part 1 (YTD backfill of runs, `backfill.py`, R23). Open: S7b day status (wave B5) |
| **M5 Official PnL and reconciliation** | Official PnL loaded; ours vs official per day, book, instrument; break analysis with causes and confidence; break register | S8, S10, S11 | After §0 steps 1–4 (wave B4); needs adapter A3 |
| **M2'' Desk rules** | OCP, cash, funding on real data: generic engine per `ORG_CONVENTIONS.md`, the organization's rules from the private adapter | S9 | **Unblocked on decisions**: DEC03 is answered by the organization's rules (term threshold open). Next: §0 steps 1, 3, 4 on wave A (P1, P2, P8) and wave B (B1–B3); DEC01 and DEC06 are needed for acceptance, not to start |
| **M8 Internal market data** | Internal EOD overlay, second EOD run, diff and notification | S10 | Needs the export format (owner, adapter A5) |
| **M4 Intraday** | Intraday runs with explain on every snapshot; SSE; later trades every 1–2 h | S12, S13 | S12 built (intraday runs every 30 min, R11, in review); SSE open; S13 waits for the trade export |
| **M6 Derivatives in core** | Derivatives valued by core next to bonds (hybrid book): canonical item §2a | S14 | §0 step 2; wave A package P3 |
| **M7 Operate** | Alerting, backups, runbooks, remaining screens migrated, `window.DESK` removed | — | |

## 2a Canonical item: derivatives valued by core (M6)

One item, previously listed under five names: [VERTICAL_SLICES](VERTICAL_SLICES.md) S14, milestone M6, the OTC part of R8 phase 2 ([OWNER_REQUESTS](../program/OWNER_REQUESTS.md), notes on R8; [ORCHESTRATION_PLAN](../program/ORCHESTRATION_PLAN.md) wave 2), core `docs/BACKLOG.md` #107 (result v3 in the desk), and backlog.json CORE-03 (core engines; done) with the derivatives share of DESK-03. Those places point here; the status is kept here only.

- **Today** (`backend/src/desk/valuation/positions.py`): an FX forward is valued at the report's value and enters FX delta only, with no interest-rate risk; swaps, futures, CDS and other OTC rows take the report's PV and DV01, the DV01 in a single bucket by maturity. Core's engines for swaps, FX forwards and swaps, XCCY, futures, repo and CDS (core 0.19–0.29, result contract v3) are not used by the monitor. Status: **not started**.
- **Target:** swaps, FX forwards and NDF, FX/rate/OFZ futures (then XCCY and repo) valued by core `value_portfolio` with result v3 stored next to v2; risk by curve and bucket, including the interest-rate risk of FX forwards and futures; the reported value kept alongside with a computed-vs-imported flag, and the difference published while each product migrates ([04](04_BACKEND.md) §7); CDS stays imported until a quote source exists.
- **Inputs:** interim terms parsed from the positions report (direction, fixed rate, index, tenor and start for swaps; amount, value date and NPV for FX forwards), then the trade-terms export (adapter A9); hub snapshot inputs H11 (futures prices), H13 (FX swaps and value dates), H12, H10 ([briefs/HUB.md](briefs/HUB.md)); core items #46, #49 (P7).
- **Work:** §0 step 2 (with the owner), wave A packages P3 (monitor), P6 (hub), P7 (core).
- Not part of this item: non-RUB bonds in core (R8 phase 2, bond part; wave A P5).

## 3 Work by repository

### trades-data-adapter (private)

Its own `ROADMAP.md` holds the detail. Phases, in the order the backend needs them:

| Phase | Outcome | Integration point (08 §11) |
|---|---|---|
| A0 | Repository, rules (no real data in Git), SPI v1 skeleton loadable from a path (`DESK_PRIVATE_ADAPTER`) | I1 |
| A1 | Metadata profiler v2 over all report families at once; the owner runs it at work and returns `metadata.zip` | — |
| A2 | Positions adapter (bonds and FX), security-id and book mapping | I2 (S7a) |
| A3 | Official PnL adapter (the official trade-level PnL report), reusing pnl_tool's hardened reader | I3 (S8) |
| A4 | OCP and cash adapter | I4 (S9) |
| A5 | Internal market data: agree an export format with the owner, then its adapter | I5 (S10) |
| A6 | Trades adapter, once the hourly export exists | I6 (S13) |
| A7 | Settings tabs of the adapter (entities, books, funding, prices); the organization's funding rules as data for the backend's generic engine | New: SPI extension (08 to be extended by P1); §0 step 1 |
| A8 | Cash balances per book, with history; cash breaks between systems | I4 (S9); §0 step 4 |
| A9 | Trade terms of derivatives and REPO, HTM contract rates → `trade_terms` (new dataset) | New (08 to be extended by P1); §0 step 2 |
| A10 | Bonds with an internal (model) price → a "sticky price" policy (`price_policy`) | With A5, I5 (S10) |

Status (22 October): A0 and A2 are done (positions at work); the OCP adapter (A4) is ready on synthetic files and waits for the `ocp` schema (P1); the official PnL reader (A3) is ported, its source still a stub; A5 is on hold by the owner's decision; A1, A6–A10 are open.

### trading_desk_monitor: backend

1. **Private input contract v1** (`contract/private/v1/`, [08](08_PRIVATE_ADAPTER_INTERFACE.md) §10): canonical `positions`, `cash_balances`, `ocp`, `pnl_official` (components: accrual, price, FX, fees, reserves; trade and position keys; revision), `trades`, `internal_market`. Plus the items already planned for 1.4: sparse Hessian, coverage and provenance states, lineage fields.
2. **`desk.inbox`** per [08](08_PRIVATE_ADAPTER_INTERFACE.md): adapter loader from `DESK_PRIVATE_ADAPTER`, folder watcher (polling; a file counts once its size and time are stable), SHA-256 ingest ledger and archive, reference adapter, conformance kit `desk adapter check`, events table; `desk inbox scan|status|replay`.
3. **Store**: bitemporal tables for the new datasets (positions exist since S5); events; break register.
4. **Orchestrator: the day cycle** (§1) as a dependency graph, not a cron list: an arrival triggers the runs that depend on it, and a rerun supersedes. `desk backfill --from 2026-01-01`: EOD runs and flash PnL for every date with positions, then reconciliation for every date with official PnL.
5. **`desk.pnl.reconcile`**: ours vs official per date × book × instrument, matched on trade or position keys. Break analysis as a substitution waterfall: swap in internal market data (Δ market data), official positions (Δ positions), CBR FX (Δ FX basis), cash flows and accrual convention (Δ carry), new-trade lines (Δ new trades); the remainder is unexplained. Each cause gets a confidence: *confirmed* (the swap closes the break within the DEC12 threshold), *likely*, *unexplained*. Component matching: our carry vs official accrual, our price effect vs price change, FX vs FX.
6. **`desk.marketdata.internal`**: the internal overlay replaces hub inputs it covers (curve points, bond prices, FX), keeps the rest, and records which. The EOD-internal run is a separate run type with its own lineage.
7. **`desk_rules`** (`backend/src/desk/rules/`, location OQ-03; the funding engine and its reference-table reader are built in wave A P2 and wired into the runs in B1): OCP, cash, funding, dual FX basis: methodology in `ORG_CONVENTIONS.md`, the organization's rules (modes, rates, spreads) as data from the private adapter (S9). Since B1 the runs compute funding (`publish/funding_run.py`).
8. **Explain** on every snapshot (§1); intraday runs; SSE (S12).
9. Carried from S4–S6: interface catalogue (`coverage`, `totals`), curve-spec mapping table, T3 presets, historical scenarios, custom scenarios, job cancel, worker out of the API process.

### trading_desk_monitor: frontend

Real-data screens come first; the remaining prototype screens migrate after them.

1. **Day status**: per date, what has arrived (positions, OCP and cash, hub EOD, internal data, official PnL), which runs exist and which superseded which; the event feed and notifications.
2. **PnL vs official**: YTD cumulative ours vs official by book, daily bars, the gap line, drill-down to instrument.
3. **Reconciliation**: the break waterfall per date and book, causes with confidence, instrument drill-down; the user sets a break's status (ours, finance, market data, timing, accepted) and a comment, kept in the break register.
4. **Market data diff**: hub vs internal per curve point, bond price and FX rate, with the PnL impact of each difference.
5. **PnL explain**: per snapshot, carry / rates / spread / FX / new trades / residual (from the prototype Scenario & PnL Lab).
6. Desk Monitor in React with OCP and cash; then Pivot Studio, Widget Board, Bonds ([05](05_FRONTEND_INTEGRATION.md) §4). Since 22 October 2026 these prototype screens are deprecated in the UI; the monitors (`prototypes/concepts/`) are the base ([05](05_FRONTEND_INTEGRATION.md)).
7. Live updates over SSE; run badge with age and coverage everywhere.
8. **Aggregated risk table: currency × instrument type** (backlog UI-02), in the monitors: currency is the top level, instrument type the breakdown (RUB total, of which OFZ-PD, corporates, swaps, …; USD total, of which corporates, futures, sovereigns, …), with the risk the monitor reports per position summed (DV01 by curve and total, CS01, OFZ basis, FX delta, MV). Each currency on its own curve and risk ("A currency's curve is its own", R27): no currency falls into RUB.

### market-data-server (hub)

The list with checks is [briefs/HUB.md](briefs/HUB.md). Done: H0–H5 (snapshot layer in production, consumer tokens), H16 (calendars to 2037), H20 (`hub_ingested_at`), H22 (`RUSFARIND` units), H26 (EOD snapshots from 2026-01-05, hub WEB-11), intraday cuts (S12), the history API (WEB-15); contract 1.3 (WEB-16, WEB-18) is in hub v0.3.2, production is at 0.2.0. On the critical path now (wave A P6, §2a):
- H11 futures prices and H13 FX swaps / swap points with value dates in the market snapshot (today FX swaps are excluded with reason `fx_swap`);
- H12 RUONIA index and declared gaps, H10 CPI levels, SARON, H17 publication calendars;
- H10–H15, SARON and H17 are entered in the hub's own `ROADMAP.md` (section "TDM H10–H15, SARON, H17", hub branch `claude/blissful-ramanujan-ukia51`, 22 October), with their state checked against the hub's code.

Bank-internal sources are not hub work: they enter through the private adapter.

### rub-fixed-income-core

Plan: `rub-fixed-income-core/docs/PRODUCT_ROADMAP.md`. Desk items: [briefs/CORE.md](briefs/CORE.md); C1, C4, C7, C8 are done in 0.33 (C5 in part; core `docs/reviews/CORE_REVIEW_FOR_DESK_MONITOR.md` §6); C2, C3 (release tags), C6 remain. Core 0.34.0 (commit `15f63e3`, foreign bonds on their own currency's curve) awaits the monitor's pickup: core BACKLOG #132, wave A P5. Wave A P7: #46, #49, #40, #103, #113, #102 (§0.2). Core #107 (result v3 in the desk) is the canonical item §2a. Reconciliation may add accrual-convention items once breaks are classified.

### pnl_tool

Stays local and unchanged. Its XLSX reader, SHA-256 idempotent import, snapshot-revision logic and "no silent zeros" rules are copied into the private adapter (A3, owner's consent 19 October 2026), not imported.

## 4 Owner answers and open questions

| ID | Question | Status | Consequence |
|---|---|---|---|
| Q1 | Where do positions and trades come from? | **Answered (19 October 2026)** for the daily book: a morning positions report and a separate OCP and cash report, dropped into folders. Intraday trade exports (every 1–2 h) will come later | Folder ingestion through the private adapter (ADR-008); without trades, intraday reprices the opening book |
| Q2, Q2a | Hosting | Answered: Windows inside; one hub on the VPS | [ADR-001](adr/ADR-001-hosting-topology.md) |
| Q3, Q6–Q9 | Licensing, products, Python, official PnL, frontend | Answered (16 October 2026) | Unchanged; official PnL = the official trade-level PnL report on the CBR basis; its composition is in the private adapter (`docs/ORG_RULES.md`) |
| Q4 | Proxy facts | **Open** | Blocks the snapshot pull from inside; the drop folder is the fallback |
| Q10 | Which date does the morning positions report describe? | **Answered (19 October 2026):** positions as of 00:00 of D, so the close of the previous business day (`time_basis = start_of_day`, 08 §6.5). Whether positions opened and closed within a day appear is unknown | The morning file reruns the EOD of D−1. Intraday round trips: the profiler (A1) looks for zero-quantity rows; otherwise they reach us only through official PnL or the trade feed |
| Q11 | Format of the internal EOD market data | **Open**; the adapter repository proposes a template (A5) | S10 |
| Q12 | Folders, file names and how far back they go | **Moved to the adapter repository** (A1, the profiler collects it) | Backfill range (S8) |
| Q13 | Can pnl_tool's reader code be copied into the private adapter repository? | **Answered: yes** (19 October 2026) | A3 copies it |

**Financial decisions on the critical path** ([DECISIONS.md](../program/DECISIONS.md), canonical for the generic IDs; the organization's answers are in the private adapter): DEC01 funding base, DEC02 accrual bases, DEC06 OCP transfers (§0 steps 1, 3, 4); DEC03 REPO cash leg is **answered** (term threshold open); DEC05 PnL attribution, DEC11 restatements, DEC12 break thresholds (M5); DEC04 partly answered (FX at 19:00, R26); DEC08 as before. Contradictions between documents are open questions OQ-01 (number of funding modes), OQ-02 (HTM base), OQ-03 (rules module location), OQ-04 (FX delta and the currency funding liability).
