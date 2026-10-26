# Desk backend (`desk`)

The Trading Desk Monitor backend. Design: [`docs/architecture/04_BACKEND.md`](../docs/architecture/04_BACKEND.md). Current state: **slices S1–S6** (next: S7a, the real book through the private adapter; see `docs/architecture/ROADMAP.md` §0) of [`docs/architecture/VERTICAL_SLICES.md`](../docs/architecture/VERTICAL_SLICES.md). It pulls verified snapshots from the VPS hub into a local cache, values every bond of a book (CNY, USD and EUR bonds on their own currency's curve) and the FX forwards, NDFs, swaps and crosses with trade terms through core 0.35.0 (pinned by commit from its GitHub repository) or, where core cannot, by a flagged approximation (see "Valuation quality"), explains the day-over-day flash PnL by exact repricing, and publishes immutable EOD runs into the desk store (PostgreSQL), which the desk API serves to the Desk Monitor prototype and to the Scenario Lab, the first React screen (`frontend/`). A scenario is estimated by Taylor in the browser from the run's stored risk and repriced exactly by core on the run's own snapshot.

```
VPS hub ──► desk.marketdata.hub + cache (desk sync) ──► snapshot cache (or a hand-filled drop folder)
                          │
hub snapshot files ──► desk.marketdata (bundle + adapter mdhub_v1)
                          │
                          ├─► core Instrument, MarketSnapshot: KBD rub_ofz_kbd_v1, RUONIA OIS rub_ruonia_ois_moex_v2,
                          │   key-rate projection rub_key_rate_projection_v1, fixings (desk.curves.rates)
                          ▼
                       desk.valuation.bonds ──► core value_portfolio (fixed, floaters, perpetuals to an assumed call)
                          │                     hash-verified result in the store   
                          ├─► desk.valuation.fallback ──► market value, stale or model price, par proxy (flagged)
                          │
                          ├─► desk.pnl.flash ──► previous date revalued, PnL by carry / rolldown / rates / spread
                          │
                          ├─► desk.publish.desk_run ──► desk store (PostgreSQL) ──► desk.api (desk serve) ──► Desk Monitor
                          │        positions as of D known at T; carried model spreads ◄──┘      │      Scenario Lab (/app/)
                          │                                                                       ▼
                          ├─► desk.scenario: Taylor (T1, from the run's risk) and exact jobs (T2, core value_portfolio scenarios)
                          └─► desk.report.validation ──► HTML/CSV: our analytics vs MOEX
```

## Run it locally

Install on Windows (the work PC, offline bundle built at home): [`docs/INSTALL_BACKEND_WINDOWS.md`](../docs/INSTALL_BACKEND_WINDOWS.md).

Requirements:
- [uv](https://docs.astral.sh/uv/). `uv sync` fetches core from its GitHub repository at the commit pinned in `pyproject.toml`; no core checkout is needed.
- The hub's address and a token of scope `read` (snapshots and, from hub 0.2.0, the history API; a `snapshots` token serves the snapshots only: see Settings). The tests need neither; they run on vendored fixtures and a local stub hub.
- To develop core and the backend together, install your core checkout over the pin: `uv pip install -e ../../rub-fixed-income-core`. Do not commit that.

```bash
cd backend && uv sync --python 3.11 --extra dev
```

Copy `.env.example` to `.env`, set `DESK_HUB_URL`, put the token file in place, then check the hub:

```bash
cd backend && .venv/bin/desk config && .venv/bin/desk hub-check
```

Pull the five latest published dates into the cache (`--date D` or `--from D1 --to D2` also work):

```bash
cd backend && .venv/bin/desk sync --last 5
```

Create the desk store (`DESK_STORE_URL`; unset, an embedded PostgreSQL in `.devdata/desk_pg` from the `pgserver` dev package, which keeps running for later commands):

```bash
cd backend && .venv/bin/desk db-migrate
```

Run EOD for each date, oldest first. `--book` loads the file into the store for that date first (an identical file adds nothing); without it the run values the books already in the store, carried forward from their latest earlier date. A load covers the books of its file only, the other books are kept; `desk load-positions FILE --date D --books OFZ,CORP` declares the books it covers, and a covered book without rows in the file is closed for the date (an empty set, carried forward). A position id twice in the file, or already in another book, rejects the whole file. `desk eod --books OFZ` values only those books as a TEST run, never the default run of the date (`--run-type EOD` with `--books` is refused):

```bash
cd backend && .venv/bin/desk eod --date 2026-10-15 --book examples/test_book.csv --report-dir ../.devdata/reports
```

`desk runs` lists the store's runs. Serve the desk API and the pages on the store:

```bash
cd backend && .venv/bin/desk serve
```

Then open:
- http://127.0.0.1:8100/concepts/index.html for the monitors (the main set of screens, `prototypes/concepts/`) on the latest default run (the run chip in their header switches runs); the API is under `/api/v1` (`/docs` for the OpenAPI). The earlier prototype screens Desk Monitor, Widget Board and Pivot Studio are removed (#292): until #313 their old addresses redirect to the monitors C01, C06 and C04, run selection kept (`REMOVED_SCREENS` in `api/app.py`). Only the deprecated Scenario & PnL Lab (`/scenario-lab.html`) still opens by direct URL, no page links it, until the decision on its scenarios (#107; root `README.md`, «Устаревшие экраны»);
- http://127.0.0.1:8100/app/ for the Scenario Lab (slice S6): pick a run and a preset or move the sliders, read the Taylor estimate (≈), then "Посчитать точно" for the exact repricing and its residual;
- `.devdata/reports/validation_<date>.html` for the comparison with MOEX analytics over the whole bond universe.

`desk serve` also runs the exact scenario worker, a thread that reprices queued jobs from the run's bundles in `DESK_SNAPSHOT_CACHE` (one worker per store); `desk serve --no-worker` leaves jobs queued. The scenario routes:

| Route | What |
|---|---|
| `GET /api/v1/runs/{id}/risk` | The run's stored risk per position (sparse delta, gamma upper triangle), for the Taylor in the browser (T1) |
| `GET /api/v1/bonds/history?instrument_id=\|isin=&curve=&from=&to=` | An issue's history for the «Облигации» tab (DATA_CONTRACT §6): every quoted day of the market history (price, MOEX yield and G-spread, accrued, volume) merged with the default runs that hold it (Z-spread, the desk's face), the KBD rate at the term to the offer or maturity and the spread to it (RUB bonds with known coupons; `curve` other than `RUB OFZ` has no rates); `instrument_id` is the secid, an ISIN is found through the static snapshots; an unknown issue gives an empty series, not 404 |
| `GET /api/v1/scenario-defs` | The preset scenario definitions |
| `POST /api/v1/scenario-jobs` | `{base_run_id, definition}`: queue an exact repricing (T2); an equivalent definition gets the existing job |
| `GET /api/v1/scenario-jobs/{id}` | The job and, when done, Taylor, exact and residual per position, book and total, with coverage |

`desk openapi --out ../frontend/openapi.json` writes the API's OpenAPI document, which the frontend types are generated from; a test fails when the committed copy is stale. The Scenario Lab is served from the committed `frontend/dist`, so running it needs no Node. To rebuild it after a change in `frontend/` (Node 18 or later, see [`frontend/README.md`](../frontend/README.md)):

```bash
cd backend && .venv/bin/desk openapi --out ../frontend/openapi.json
cd ../frontend && npm ci && npm run gen:api && npm test && npm run build
```

`desk eod` reads only the cache, so a run never depends on the hub being up; `desk eod --sync` pulls the date and the business date before it first. Every run prints the market snapshot ID, where it came from, its `published_at` and age, and records them in the run log lineage. When the cache holds an earlier date, `desk eod` attaches the flash PnL against it (slice S2; `--no-pnl` turns it off).

Books, all synthetic (real public instruments, invented quantities; real desk positions never enter the repository):
- `examples/test_book.csv`: the S3 test book. Three books (`OFZ`, `CORP`, `MIXED`), long and short, with the coverage cases (OFZ-PK, a key-rate floater, OFZ-IN, a perpetual, a bond without quotes on some dates), each valued with its quality flag. Regenerate it with `desk demo-book --profile test --date D --out examples/test_book.csv`;
- `examples/demo_book.csv`: the S1 demo book (`desk demo-book`).

### Bank reports (slice S7a)

Positions arrive as bank reports read by the private adapter (`docs/architecture/08_PRIVATE_ADAPTER_INTERFACE.md`; unset, the reference adapter over canonical CSV). Set `DESK_INBOX_DIRS` (and `DESK_PRIVATE_ADAPTER`, `DESK_PRIVATE_ADAPTER_CONFIG`), then:

```bash
desk adapter check                 # the conformance kit over the inbox folders, without the store
desk inbox scan                    # new files into the ledger and the store; prints counts by status
desk inbox status --date 2026-10-15   # files, units and issues per source and business date; book ages
desk inbox reconcile               # per book: rows of the report -> accepted -> in the default run; the losses
desk eod --date 2026-10-15         # values the inbox positions; prints each book's source and age
```

`desk inbox reconcile [--date D] [--json]` (R22) follows each book's rows for a business date (default: the latest
default run's): the position set in force (valid date, report, file), the rows of the report unit that stored it,
the rows of the set, the rows of the date's default run and how many were valued, the book's units rejected or
superseded from that set's date on with their issue codes; then the files by status, the error types and every
loss code with its meaning (`match_failed`, `timed_out`, `waiting_calendar`, `ignored_too_old`, a rejected book,
`scope_conflict`, `book_closed_by_omission`, `duplicate_key`, `unknown_book`, positions loaded after the run). The
settings page shows the same as «Загрузка позиций» (`GET /api/v1/inbox/reconcile`, loopback only); the diagnostics
carry its totals and codes without books or files. `tools/bench_inbox.py` (dev) times a scan of a synthetic folder
like the owner's (2,500 files, a slow adapter, a 20,000-bond static).

`desk inbox retry --status failed,rejected` offers files again; `desk inbox replay --source S --from D1 --to D2` re-reads archived accepted files and stores a new revision only where the rows changed. When the adapter fails to load, ingestion is off with the reason (`desk inbox status`, `/health/ready`), and runs and the API keep working.

### Daily flow (autopilot)

`desk serve` runs the autopilot (`desk/autopilot.py`), a thread that every `DESK_INBOX_POLL_SECONDS` (60), and at once on `POST /api/v1/autopilot/run`, runs one cycle in this order:

1. **sync**: the `autopilot_sync_last` (5) latest hub dates, every business date with positions in the last two weeks and the hub date before it (flash PnL), and a snapshot whose calendar covers each inbox file still waiting for one; cached dates are skipped (asked again once an hour, for corrections). Never a date older than the inbox lookback (`inbox_lookback_days`, 35; the hub date before its first day apart), at most `autopilot_sync_last` + 3 dates per cycle, and a date the hub has no snapshot for is not asked again for 6 hours. The drop folder, when set, is imported too. A hub error is shown and the cycle goes on with the cache. Then every bundle of the cache not loaded yet goes into the store's market history (see "Market history"; a cycle with nothing new costs one query).
2. **scan**: `desk inbox scan`. It comes after the sync because a start-of-day report needs a cached calendar covering its date and the day before (otherwise the file waits, `waiting_calendar`, and is retried next cycle). Within the lookback only, by report date (R22): files whose report date is older are remembered once as `ignored_too_old` and never read, waiting files whose report date falls behind get that status too, and only files modified more than 31 days before the lookback starts are skipped on their modification time alone (never matched or hashed); a folder of years of reports costs a directory listing (one `os.scandir` per folder: on Windows it carries sizes and times, no request per file on a network share). Matching: the adapter's workers are started and warmed first (import, instruments index) under the read timeout, then the batches of 50 files go to all workers at once, each under `DESK_ADAPTER_MATCH_TIMEOUT_S`; the files of a call that timed out or crashed are matched once more with 4 times the time (at least 60 s), 10 per call after a timeout and one per call after a crash, and a file still failing that way is offered again 6 hours later. The instruments index holds the newest static snapshot in full and the older ones as differences (format 2: five weeks of a 20,000-bond static is ~6 MB instead of ~120 MB) and is rebuilt only when it lacks a snapshot. The backfill (step 6) reads the older reports once, from its own start date.
3. **eod**: for each cached `eod_final` date of the last two weeks (at least the latest date with positions) that has positions, the `desk eod` run when the date has no default run, or its default run read other position sets (a new revision), another market snapshot or another previous snapshot; the latest date with positions also when its default run was made by another program version (`desk.logic_version()` compared without its `+build` part, `backfill.same_logic`: a new backend version, the hand-bumped `VALUATION_VERSION` or a new core version recompute it once; a new Windows bundle of the same code does not). Oldest first; a date that failed is retried only when its inputs change. While the inbox drains over several cycles (the scan left files waiting for a calendar and the sync left hub dates for the next cycle), only the latest positions date runs; the older dates run once the backlog is in, not once per cycle as each book's revision lands (R22). The older dates are the backfill's (step 6).
4. **intraday** (with a hub and `intraday` on, the default; R11): on a business day from 10:00 MSK, today's book (the latest position sets, carried forward) valued on the hub's newest `intraday` cut of today, and from 19:45 on its `eod_preliminary` cut: the `desk eod` code path on another market snapshot, an `INTRADAY` run (`cut_type` intraday; a preliminary one is an EOD run, the date's default until the final). The hub is asked (one list call, `latest=true`) at most once per 10-minute slot (:05, :15, ... ; #20) by the intraday step itself, so a failed sync of an older date does not hold it back, and for the preliminary cut only until today has a run on it; a run is made only for a cut newer than the last run's, or when the position sets changed. The flash PnL is against the previous business day's `eod_final`; when the cache lacks it, against the newest earlier `eod_final`, else the newest earlier `eod_preliminary`, and the run says so (`lineage.pnl_basis`, `pnl_basis` in the API: «PnL от 15.10 (закрытие): нет закрытия 16.10»); the EOD runs follow the same rule. Only the latest `INTRADAY` run of a date is kept: the older ones are deleted with their rows once a newer one is complete (migration 0010 lets only those be deleted; a run a scenario job was submitted on stays). Intraday and preliminary bundles of dates older than 3 days leave the snapshot cache, unless a run still shown reads them. The next morning the date's `eod_final` run (step 3) supersedes both. Runs record `cut_ts` (the cut's nominal instant) and `prev_cut_type`.

   **morning** (#21; the hub is not needed): on a business day from 07:00 MSK (earlier once a current position set has report date today, as today's `start_of_day` report, valid the day before, or is valid today), until today has a run on its own market, today's book is valued on the previous business day's close in the cache (its `eod_final`, else its `eod_preliminary`): an `INTRADAY` run of today with `cut_type` `prev_close`, `lineage.market_close` `{date, cut_type}`, no flash PnL (`pnl_basis` says why) and the label «16.10 · закрытие 15.10». It is made again only when the position sets, the close or the valuation logic change (the older one deleted), and the first intraday cut's run replaces it. While the inbox drains (as step 3, R22) it waits. Without the previous business day's close in the cache there is no morning run, even when an older close is there (#21 asks for D−1's close; the header then keeps the last run). The `intraday` setting does not turn it off. The step's `reason`: `not_business_day`, `before_morning`, `no_positions`, `no_close`, `superseded`, `up_to_date`, `inbox_backlog`, `ran`, `failed_before`, `run_failed`, `error`; the latest run in `/api/v1/autopilot/status` carries `market_close`.
5. **history** (with a hub and `history_days` > 0): the market history beyond the cache for the bond charts, 5 of the `history_days` (260, about a year) latest hub dates per cycle, newest first, straight into the store without caching the bundles. Only after a sync that reached the hub and left no date for the next cycle, and after the EOD runs; the inbox lookback does not apply (these dates feed no run). The hub's date list is read once an hour.
6. **backfill** (`backfill` on, the default; R23): the default runs of the past business dates, see "History of runs". Last in the cycle and at most `backfill_budget_s` (600) seconds of it, so the latest date never waits; up to the day before the eod step's first date; not while the inbox backlog drains.

The run pages open on is the newest run shown (`store.CURRENT_RUNS`): per date its default run, otherwise its intraday run, so during the day today's intraday run. `GET /api/v1/runs/default` without a date returns it, `GET /api/v1/runs` marks it `is_current`, and every run header carries its slice (`cut_type`, `cut_ts`, `label`: «20.10 14:30 МСК · внутри дня», «19.10 · закрытие предв.», «19.10 · закрытие») and `pnl_basis`; `window.DESK.source` has `cutType`, `cutTs`, `slice`, `prevCutType` and `pnlBasis`, and the shared nav bar names runs by their slice.

Settings are read again every cycle. `GET /api/v1/autopilot/status` gives the state, the steps of the last cycle (the intraday step with a machine-readable `reason`: `off`, `before_open`, `not_business_day`, `no_positions`, `no_cut_yet`, `up_to_date`, `ran`, `failed_before`, `run_failed`, `hub_error`, `error`; the sync step with `failed_dates`, the hub dates that failed alone, #20) the latest run shown with its slice, the server's clock (`now`) and the intraday polling schedule (`schedule`: first cut, preliminary cut, slot and lag, the 10:15 warning time) that the monitors' header uses for «следующее обновление» and its no-cut warning (#22); errors are also logged to `<home>/logs/autopilot.log`. `desk serve --no-autopilot` starts no thread; `--autopilot` runs cycles even when the settings turn it off. `desk cycle` runs one cycle in the foreground and prints it (exit 1 when a step failed; `--date D` also keeps D current, `--no-hub` skips the hub); the Windows bundle's `update_daily.bat` calls it (its arguments go to `desk cycle`). One cycle at a time per home folder, across processes: a cycle holds `<home>/cycle.lock` (`msvcrt.locking` / `fcntl.flock`, released by the OS if the process dies); a cycle that finds it taken does nothing and reports the step `cycle: skipped` (another cycle running).

### History of runs (backfill, R23)

The risk, MV and PnL series of the pages, PnL month and year to date, and the history pages are sums of the default runs, one per business date. Until R23 runs reached about two weeks back: the eod step keeps the last 14 calendar days current (at most 10 dates), the sync pulls the hub's bundles only for the dates with positions in that window, the inbox reads reports at most `inbox_lookback_days` (35) old, and an upgrade recomputes only the latest date. Nothing computed an older date.

`desk backfill [--from D] [--to D] [--dry-run] [--no-hub] [--no-inbox] [--rescan]` (`desk/backfill.py`) and the autopilot's backfill step compute them:

1. **reports**: the inbox scan once more, its lookback starting at `--from` (default the setting `backfill_from`, 2026-01-05, the hub's first `eod_final` close): the reports older than the regular lookback (`ignored_too_old`) are read into position sets of their dates. Once per start date and adapter fingerprint (`<home>/backfill.json`); `--rescan` reads them again, `--no-inbox` skips it.
2. **plan**: the business dates from `--from` to `--to` (default today) that the hub has an `eod_final` close for (the cache's dates with `--no-hub` or without a hub). A date's positions are `store.positions_for` of the date: per book the newest position set valid on or before it, so a book without a report that day is carried forward from its last report, as in every run; a date before the first report of every book has none and no run. A date needs a run when it has no default run, or its default run read other position sets, another market snapshot (a hub correction), another previous close, or was made by another `logic_version()` without its `+build` part (a new `VALUATION_VERSION`, backend or core version recomputes the history; a new Windows bundle of the same code does not). Otherwise it is skipped. `--dry-run` lists the dates to run, why, and how many bundles are to be downloaded.
3. **runs**, oldest first: the date's `eod_final` bundle and the previous close (flash PnL) from the hub when not cached, then the `desk eod` code path (`cli.eod_run`). A prepared market is kept for the next date (`cli.ContextCache`: each date's static snapshot and curves are built once, not twice). One date at a time: a model price carries its bond's spread from the latest earlier run (`store.carried_spreads`), so a date depends on the dates before it and dates in parallel would compute other numbers. A failed date is not tried again on the same inputs (`<home>/backfill.json`); an error of the store connection, the network, the disk or the hub as a whole (`backfill.transient`) is not the date's: the run stops there and the date is computed next time (E2E 21.10.2026: a store that went away mid-run had left 20 dates failed for good). The command takes the cycle lock (`<home>/cycle.lock`) per date, so a `desk serve` alongside keeps its cycles between dates; it prints each date with the time left.

The bundles stay in the snapshot cache: a market bundle of the whole universe is ~0.5 MB and its static snapshot several MB, about 1–2 GB for January to October. The cache keeps the bundles' envelope fields in `.manifests.json` (`bundle.MANIFEST_INDEX`), so a new process lists it without parsing every bundle (live E2E: 190 bundles, 54 s → 0.8 s at the start of each `desk` command and of «История прогонов»); a file added, changed or removed is seen, a folder that cannot be written works as before. Progress: the settings page's «История прогонов» (`GET /api/v1/backfill/status`: dates with their run, to run, without positions, failed, the oldest run), the autopilot status (`progress.step` `backfill`: «История прогонов 12.03.2026: 5 из 140») and the diagnostics' `backfill` section. The backfill's runs do not write the static facts of their snapshots (`static_facts`: the latest date's do).

### Diagnostics for the agent (R13)

The owner's desk runs on real positions the agent never sees; the diagnostics export is how a problem there is read remotely. The settings page's «Диагностика» («Собрать диагностику», then «Скачать» or «Скопировать»), `GET /api/v1/diagnostics` (`?hub=0` skips the hub, `?download=1` returns it as `desk-diagnostics-<UTC>.json`; this computer only, like the settings routes) and `desk diagnostics [--out FILE] [--no-hub]` (Windows: `desk.bat diagnostics --out diag.json`) build one JSON document, `schema: "desk-diagnostics/1"` (`desk/diagnostics.py`):

| Section | What |
|---|---|
| `app`, `clock` | versions (bundle, backend, logic, core), Python and OS family, uptime, `DESK_*` variables (values only of flags and numbers); UTC, Moscow and local time |
| `settings` | flags, numbers and enums as they are; paths, URLs, names and secrets only `set`/`unset` (folders: whether they exist) |
| `autopilot` | state, the last cycles with each step's status, duration and detail; the intraday window, next hub slot and what the hub answered; from `autopilot.log` the cycles of any process and per day each step's outcomes by kind |
| `hub`, `snapshots`, `static` | a live hub check and its newest snapshots of every cut (cached or not); per cached market snapshot the curve quote sets per `source/dataset` with their quote counts, the expected curves missing, ZCYC and quote counts, whether the RUONIA OIS and key-rate curves build from it; static snapshots by status, class and coupon type |
| `inbox`, `positions` | adapter name and version, files by status, source and error code, issue codes, file extensions; position sets per date and today's book in counts |
| `runs` | the last 20 runs: date, type, cut, times, versions, positions by quality and reason code, discount curve, curves built, risk per curve (positions contributing), floaters by index, coupon display counts, PnL availability, basis and reason codes |
| `backfill` | the history of runs (R23): range, dates with their run, to run, without positions, failed (dates), the oldest run; run summaries written; the static facts' snapshots with their coverage counts; CBR meetings |
| `logs`, `db` | log records by day, level and logger, the most frequent warning and error kinds, the last error lines; migrations and table row counts |

No ISIN or SECID, book, portfolio, trader or counterparty, amount, price or PnL value, file path, user or host name or token: sections hold codes, dates, counts and snapshot id prefixes, and every free text (step details, errors, log lines) and finally the whole document are scrubbed of ISIN-like tokens, paths, URLs, addresses, amounts and every sensitive value the settings and the store know (`tests/test_diagnostics.py` checks it on a fixture run).

### Offline fallback

When the hub is unreachable (the proxy blocks it), an operator copies `<snapshot_id>.json.gz` bundles into `DESK_SNAPSHOT_DROP` and runs `desk import-drop`. Each file is verified like a download; a bad file is rejected with its reason and not copied.

Without any hub, the dev export restores the hub seed into `.devdata/pg` and exports real `eod_final` snapshots for 2026-10-09 … 2026-10-15 to `.devdata/snapshots/` (about 3 minutes). It needs the sibling checkout `../market-data-server` with the hub's release bundle in `dist/` and its snapshot-layer worktree:

```bash
tools/devdata/build_dev_snapshots.sh
```

### Market history (bond charts)

The price, yield and spread charts read the store's market history (`desk/marketdata/history.py`, migration `0009_md_bond_eod.sql`), not the runs alone:
- `md_bond_eod`: one quote per bond and business date from each `eod_final` market snapshot (of its boards the one with a price, in the runs' order `legal_close`, `close`, `waprice`, then the most trades), with the MOEX effective yield to its yield date, duration, G- and Z-spread, accrued, volume;
- `md_kbd`: the date's KBD parameters and the snapshot the date's rows come from. The newest snapshot of a date (latest `known_at`) wins: a correction replaces the date's rows;
- `md_instrument`: ISIN, coupon type, face currency and maturity from the newest static snapshot;
- `md_snapshot_load`: every snapshot id handled, so a pass with nothing new costs a folder listing and one query.

It is filled from every bundle of the cache (the autopilot's sync step; `desk sync` and `desk import-drop` too when `DESK_STORE_URL` is set), from the snapshots a `desk eod` reads, and by the autopilot's history step for the `history_days` latest hub dates the cache does not hold. Those bundles are not cached: for the whole universe a market bundle is about 0.5 MB and its static one about 4 MB (the fixtures' bytes per bond, about 140 per quote and 1 200 per bond's terms, times ~3 000 bonds), so a year of them would take about 1 GB, while a year of rows takes about 100 MB. The history is a read model: a lost store refills from the cache and the hub.

### Deep history from the hub's history API (R24)

Hub 0.2.0 serves its stored tables as columnar history (`/v1/history/index|bonds|curves/{dataset}|zcyc|fixings|fx`, the hub's `docs/HISTORY_API.md`; a token with scope **`read`**, which also opens the snapshots). When the hub has it, the autopilot's history step reads the history from there instead of one bundle per date (`desk/marketdata/hubhistory.py`, migration `0011_md_hub_history.sql`):
- tables `md_hub_bond` (bond EOD on the bond's main board: prices, MOEX effective yield, G- and Z-spread, duration, accrued, turnover, the linkers' ICPI / IR / BEI / CBR of contract 1.2), `md_hub_zcyc` (KBD per day), `md_hub_curve` (the end-of-day quote set of each day and source of every curve dataset the index lists: `ruonia_ois`, `key_rate_swap`, the CNY curves), `md_hub_fixing` (KEY_RATE, RUONIA, RUSFAR rates and the RUSFARIND index, CPI_RU_MOM, …), `md_hub_fx` (CBR official and MOEX EOD); numbers arrive as `Decimal` (rates and fixings stored as `numeric`);
- depth: bonds from `history_bonds_from` (2024-01-03: the hub's first bond EOD), the rest from `history_rates_from` (2010-01-01, bounded by the index); the whole market, the desk's bonds (the default runs of the last 10 dates) first;
- incremental: one query (*job*, `md_hub_job`) per closed month of bonds and per closed year of the rest, plus a *tail* from the first day of the month 14 days back to today. The tail is revalidated every 10 minutes and each closed chunk every day with the ETag of its first page (`If-None-Match` → 304: one cheap request; the hub's ETag covers the whole selection, so a revision of any day of a chunk refetches that chunk alone). A fetch writes its rows under a new fetch id and, when complete, deletes the rows of its range the hub no longer has;
- bounded: 40 s a cycle (`hubhistory.BUDGET_S`), after the EOD and intraday steps; a query still paging keeps its cursor and goes on in the next cycle, so the latest date's run is never delayed. Measured on the synthetic stub (`tests/hub_history_stub.py`, 10 bonds from 2024-01-03 and two years of curves): 61 requests, 57 000 rows, 0.7 MB gzipped in 2.6 s; the whole market (~3 000 bonds × ~700 days ≈ 2.1 M bond rows) is ~45 pages of 50 000, ~75 MB on the wire once (about 40 cycles), then two pages when the hub changes the tail and ~50 conditional requests a day;
- fallback: a hub before 0.2.0 (404 on the index: «hub без API истории (версия < 0.2.0)») or a token without `read` (403: «токен без права read») makes the step load bundles as before (`history_days`), with the reason in the step's detail, `md_hub_state`, the settings page («Маркет-дата сервер» → «Проверить связь»; «Автообновление» under «История котировок»), `desk hub-check`, `desk history status` and the diagnostics (`hub.history_api`, `market_history`). The API is asked again after an hour, or at once after a new token is saved;
- readers take a day's row from the views `md_bond_day` and `md_kbd_day`: the history API's row when it has one, else the snapshot's.

`desk history status [--json]` prints the stored ranges and the queries; `desk history sync [--budget S]` fetches now (resumable, 600 s by default).

### Concept pages on real runs

The interface concepts (`prototypes/concepts/`, served at `/concepts/`) read the run the other pages show. `lib/boot.js` chooses the data before the page code runs: over `desk serve`, `/concepts-run.js` (`window.CONCEPTS_RUN`, #291) and `/concepts-data.js` (`window.DESK_EXTRAS`), from which `lib/real.js` builds the pages' data (the shape of the demo set `lib/mock.js`); `?demo=1`, a store without runs or a page opened as a file get the demo set, and the header says so. The run is chosen as on the other pages (`?run_id=`, `?as_of=`, the remembered choice `desk.run.v1`), from the header's run chip too.

- `/concepts-run.js?run_id=` (`window.CONCEPTS_RUN`) and `/api/v1/concepts/run?run_id=` (`desk/api/concepts_run.py`, #289): the run as `lib/real.js` reads it, `window.DESK` cut down to those keys (dates, PnL state, buckets, curves, FX, indices, issuers, books, risk notes, funding status, source, positions); the run chosen as for `/desk-data.js`, built ahead and cached the same way; no run: `window.CONCEPTS_RUN_EMPTY` (a 404 from the JSON route). The pages load it instead of `/desk-data.js` since #291; compat stays for the old screens until the next steps of #107.
- `/concepts-data.js?run_id=` (`desk/api/concepts.py`): over the default runs up to the run (at most 260), the desk's series (DV01 by curve, CS01, OFZ basis, DV01 by book, market value by instrument type and currency, PnL by factor), each run's own sums; per position its PnL month and year to date over those runs and `pnlPeriods` (from which run, and whether the sum is partial: the positions start after the period's first business day, a business day of the market history has no run, or the history does not reach back; the header's notes say so and the pages mark MTD/YTD with «*»); the previous run's price and spread and the change of its holding (a trade); the positions' flows of the next 18 months; the market of the latest close in the history (every quoted bond, RUB and foreign face, with the static snapshot's issuer, group, sector, outstanding face, coupon and frequency, and the day's movers by turnover); the CBR meetings (`cbr`: the next one, possibly with an estimated date, the dates ahead, `schedule`: each meeting ahead with the date its decision takes effect and whether its date is estimated, the past decisions, the key rate). Ratings are not in the hub's data: null, listed in the notes.
- `/api/v1/history/positions?run_id=&ids=&from=`: the run's positions on one date axis, MOEX price, yield (none for a floater), G-spread and (when present) MOEX's Z-spread to the KBD (`zspr`) and a linker's `icpi` / `ir` / `bei` from `md_bond_day`, a floater's discount margin by the runs (`dm`), the KBD at the bond's term, and from the default runs face, DV01, CS01, OFZ basis, MV and DV01 by curve (carried forward between runs, null before the first); a series empty on every date is null. C11 charts the desk's risk and MV per date from these (with the book filter), not as today's sensitivities times the face. A year by default; a position's card asks for its whole history (`from=2000-01-01`: from 2024-01-03 with the history API) and only that position's part of the summaries is read, a page that charts every position (C11, C13) for all at once.
- `/api/v1/history/market?run_id=` (`desk/api/market_history.py`, loaded lazily by C05 and C09 only, ETag, cached per run and history version): as of the run's latest market day, per bond the yield (bp) and price change over 1D / 1W / 1M / 3M / YTD; КС, RUONIA, RUSFAR, RUSFAR CNY and CPI year on year with their changes and last year; CBR USD/EUR/CNY and MOEX TOM; RUONIA OIS, key-rate swaps, the KBD and the other curve datasets at the risk pillars on those days and two years of 1Y/2Y/5Y/10Y. Without the history API: the bonds from the snapshots, the rest empty with a note. `/api/v1/history/status`: `hubhistory.status`.
- `/api/v1/calendar/market?run_id=`: the market's payment calendar from the static snapshot of the run's date (`desk/marketdata/static_facts.py`, see below), aggregated: RUB by day × type (ОФЗ-ПД, -ПК, -ИН, corporate fixed and floating, subfederal and municipal, other) × kind (coupon, amortization, maturity, put, call), with the same for the large issues (≥ 10 bn RUB outstanding); RUB by month × issuer sector; the large RUB redemptions and offers one by one; the FX-face bonds of Russian issuers by day × currency × kind and by month × currency × issuer, their redemptions and offers one by one. Amounts are in the face currency, never converted or summed across currencies. With `market=rub|fx` and `from`, `to`, `type`, `kind` (`red`, `redemption`, `offer` or one kind), `ccy`, `sector`, `issuer`, `secid`, `big`, `limit` it returns the flows behind a cell, the largest first, with the totals of all that match (C08's drill-downs).
- Run summaries (`run_summary`, migration 0012, `desk/publish/summary.py`): a run never changes, so its sums and per position the numbers the series need are computed once, by `desk eod` right after the publication; a run from before R23 gets its summary on the first page load (the API writes it on a connection of its own). A year of 600 positions is then one row per run instead of hundreds of thousands of facts.
- Cached bodies: these responses and `/desk-data.js` (which also depends on the scenario definitions; still `no-store` for the browser, R16) are built once per run and store state (`concepts.fingerprint`: the complete runs, the market history's load times, the static facts, the CBR meetings) and kept in memory with their gzip and ETag (`app.BodyCache`); a page reload gets a 304, a new run or market date builds them again. `desk serve` builds the newest run's bodies ahead every 20 s when the store changed (`app.warm_bodies`), so the first page after a new run finds them ready.
- Static facts (`desk/marketdata/static_facts.py`, tables `md_static_summary`, `md_static_bond`, `md_market_flow`, `md_cbr_meeting`): `desk eod` puts the run's static snapshot's facts into the read model (hub static 1.1: issuer short name, group `group_name`, sector `sector` in Russian, `is_state_owned`, Latin name `lat_name`, `issue_size`, `coupon_frequency` else `coupon_frequency_schedule`, the current coupon rate) and the payments of every active bond for 400 days: coupons (`amount_per_bond` × `issue_size`; a coupon MOEX has not fixed yet takes the last fixed amount scaled by the face, marked as an estimate), amortizations and maturities, scheduled puts and calls (the face outstanding on the date × issue size: what could be redeemed). `issue_size` is MOEX's number of bonds of the issue (for OFZ the declared volume, not what was placed). An FX-face bond of a foreign issuer (a foreign sovereign or IFI, or an issuer the hub places outside Russia) is left out. The newest 4 static snapshots are kept. The CBR meetings come from the run's market snapshot (`cbr_meetings`). The run's issuer references carry the hub's group, the sector in Russian and `is_quasi` (`VALUATION_VERSION` 7), a bond's fact `extra.lat_name` and `extra.issue_size`.
- A bond's fact carries its offer (`extra.offer_date`, `offer_kind`), its flows of the next 18 months (`extra.cashflows`: date, kind, amount in the face currency, forecast) and the day's quote (`extra.quote`: G-spread, bid/offer when the cut has them, volume, turnover); runs from `VALUATION_VERSION` 6 (the latest date is recomputed once after the upgrade).
- Responses over 4 KB are gzipped.

`tests/concepts_live.py` serves two fixture runs, a month of market history from snapshots and the deep history of the history-API stub (bonds from 2024-01-03; `--no-api` leaves it out) on an embedded PostgreSQL; `node ../prototypes/concepts/tools/smoke.js <out> --base http://127.0.0.1:8199/concepts/` and `tools/clickthrough.js --base ...` open every concept page on it and fail on any console error.

`tools/bench_concepts.py` builds the owner's size synthetically (600 positions in 60 books, 200 runs, 3 000 bonds of market history over 260 dates, a static snapshot of 4 500 bonds) and times the data requests; `--serve PORT` keeps serving it for `node ../prototypes/concepts/tools/tti.js --base http://127.0.0.1:PORT/concepts/` (time to the end of `load` of C01, C03, C08 in its three modes, C11). On this machine (R23), before → after: `/concepts-data.js` 12.8 s → 0.8 s on the first request after a new run, 0.013 s warm (2.7 MB, 0.54 MB gzip); `/api/v1/history/positions` 5.5 s and 14 MB → 3.0 s first, 0.016 s warm (10 MB, 3.4 MB gzip); `/desk-data.js` 0.6 s every time → 0.011 s warm; all four ready before the first page asks once `desk serve` has built them ahead (4.4 s); `/api/v1/calendar/market` 0.04 s (0.3 MB), a drill 0.08 s. Pages to the end of `load` (first visit): C01 15.1 → 0.7 s, C03 20.5 → 1.3 s, C08 17.0 → 1.6 s (its market modes 15 → 0.6 s), C11 20.3 → 1.5 s.

## Valuation quality

The owner's rule (19 October 2026): a bond is valued somehow rather than not at all. Every valuation carries `quality` and `assumptions`, published in the position's `extra`, the run log and the validation report:

| `quality` | How | Example assumptions |
|---|---|---|
| `exact` | core `value_portfolio` on the published terms | none |
| `assumed` | core, with stated assumptions | floater formula `inferred` with the default convention; perpetual to an assumed call; future coupons at the current rate; accrued computed; accrued mismatch vs MOEX |
| `market_value` | today's price × face + accrued (× FX for a foreign face); risk from core calibrated to the price, else from the duration (a foreign bond's always, on its currency's curve) | the reason core could not value it; a duration proxy (foreign face, OFZ-IN indexed face) |
| `stale_price` | the same with the last price of the previous 30 days | price date and age |
| `model_price` | the bond's schedule on the discount curve (RUONIA OIS) at the Z-spread to that curve carried from the latest earlier run in the store (the spread its model price used, or its last market Z if newer), so only the curve moves it; without one, the median Z of its issuer, class or kind (traded bonds, Z within −100…2000 bp) | where the spread comes from and since when |
| `par_proxy` | current face (+ accrued if known), no risk | no price, no usable schedule |

A bond past its final redemption, or fully amortized, is valued 0. The face is the one outstanding on the business date from the redemption schedule. Only a bond with no data at all (no terms, no face) stays without a valuation, and an unexpected error for one bond sends it to `par_proxy` with the error named.

### Risk by curve

RUB bonds, OFZ and corporate, fixed and floating, are discounted on the RUONIA OIS curve plus their Z-spread
calibrated to the price (owner's decision of 20.10; ORG_CONVENTIONS §4, DATA_CONTRACT §0.3): the OFZ spread to
RUONIA (`ofz_spr01`) or the credit spread to RUONIA (`cs01`); `spread_curve` is `RUB RUONIA`. The KBD (`RUB OFZ`)
stays the market curve of the G-spread, the curve chart and the bond history: no bond carries DV01 on it. A date
whose market has no RUONIA OIS curve falls back to the KBD: every bond valuation says so in its assumptions
(`assumed`), its risk is on `RUB OFZ`, and the flash PnL across the change of curve is the simple one (all in
`other`). Bucket DV01 and the Hessian are published per curve (owner request R8); `dv01` is their sum:

| Position | Curves of its risk |
|---|---|
| Fixed RUB bond (core, or a fallback rung priced on the curve) | `RUB RUONIA` |
| OFZ-PK on RUONIA (projected and discounted on the same curve) | `RUB RUONIA`: one factor, a short rate risk to its reset |
| Floater on the key rate | `RUB RUONIA` (discounting) and `RUB KEY` (projection): two legs of opposite sign, the basis, with the cross terms in the Hessian |
| Foreign bond (fallback, core has no curve in its currency) | `USD`, `EUR`, `CNY`…: DV01 in RUB from the MOEX duration, a proxy |
| OFZ-IN (fallback) | `RUB RUONIA`, nominal DV01 from the duration, flagged as a proxy (no real curve or inflation factor yet) |
| Report's own risk (`risk_dv01_rub`: unmapped bond, OTC) | the curve of the row's currency (`RUB RUONIA` for RUB) |

The run publishes `RUB OFZ`, `RUB RUONIA` and `RUB KEY` with their points (KBD effective yields; zero rates of the RUONIA OIS and key-rate projection curves, annual compounding) and the day's move, and a curve without points for a currency's curve. A scenario shifts each curve by its own vector (`desk.scenario.exact.core_scenario`); the preset moves of all rates shift the three RUB curves alike (store migration 0008), so their total is what it was on the KBD; a shock of `RUB OFZ` alone moves no bond. The flash PnL (`desk-flash-exact-v2`) reprices each bond on the curves it depends on (a floater's coupons reprojected), so `rates` matches the linear explain, DV01 per curve x that curve's move.

Exact and assumed bonds get the flash PnL by exact repricing. Market value and stale or model price get PnL = q·(dirty_D − dirty_D−1) + received, all in `other`; across a model price it carries a flag ("price discovery: model -> market"). A position at par on either date gets no PnL (reason `par_proxy`). A foreign-face bond's FX revaluation (its FX delta of D−1 × the rate move) is taken out of `other` into `fx_gross`. Rows valued without core (`pnl/direct.py`): an FX row gets amount × (rate D − rate D−1), method `desk-fx-spot-v1` (a forward only its spot delta, flagged); a row at the report's value (OTC, unmapped bonds) gets today's reported PV − yesterday's for the same position, method `desk-reported-v1`, flag «по отчёту», shown as its own explain step; none when yesterday had no reported value or the book's report was not renewed.

A missing PnL is never 0 (R10): a position without one has `run_position.no_pnl` {reason, detail}, and the `window.DESK` adapter gives it null amounts with the reason's Russian text (`pnl/reasons.py`); a run without a previous `eod_final` market has `prevDate` null, `pnlAvailable` false with its reason, and a WARN line in its own log. The Desk Monitor shows «—» with the reason as a tooltip, and totals over the positions with PnL marked «частично: N из M позиций».

## Settings

`desk.settings` reads, the first that sets a key winning: `DESK_*` environment variables, then `<home>/settings.json`, then `backend/.env` (or `DESK_ENV_FILE`), then the defaults. `<home>` is the per-user data folder, outside the install folder so that a new version unpacked anywhere keeps the settings and the data: `DESK_HOME`, else `%LOCALAPPDATA%\DeskMonitor` on Windows, else `<repo>/.devdata` in a git checkout (today's dev layout, unchanged), else `~/.local/share/desk-monitor`. It holds `settings.json`, `hub_token`, `adapter-config/`, `snapshots/`, `inbox-archive/`, `pg/` (embedded store) and `logs/` (the dev checkout keeps `.devdata/inbox` and `.devdata/desk_pg`).

The settings page `http://127.0.0.1:8100/settings.html` (`desk serve`) edits `settings.json` (keys in lower case: `hub_url`, `hub_ca_bundle`, `hub_timeout_s`, `https_proxy`, `no_proxy`, `inbox_dirs` as `[{"path", "label", "recursive"}]`, `private_adapter`, `private_adapter_config`, `adapter_check_timeout_s`, `inbox_poll_seconds`, `autopilot`, `autopilot_sync_last`, `intraday`, `inbox_lookback_days`, `history_days`, `backfill`, `backfill_from`, `backfill_budget_s`, `snapshot_drop`, `store_url`, `update_dir`; relative paths against `<home>`), stores the hub token in `<home>/hub_token` (mode 0600; never returned by the API), checks the hub, shows the adapter's status and edits the config files it declares (`describe()["config_files"]`, 08 §4). `load()` re-reads `settings.json` on every call, so the running server picks up edits. Its routes (`/api/v1/settings`, `/api/v1/settings/hub-token`, `/api/v1/settings/test-hub`, `/api/v1/adapter`, `/api/v1/adapter/config/{name}`) answer loopback clients only and are left out of the OpenAPI document. `backend/.env` remains for development: copy [`.env.example`](.env.example) to `.env`; it is git-ignored. `desk config` prints the effective values, with the token masked. The proxy keys go into the process environment unless it sets them already: `https_proxy` as `HTTPS_PROXY`, `no_proxy` as `NO_PROXY` only when an HTTPS proxy is set too (on Windows any `*_PROXY` variable, `NO_PROXY` alone included, makes Python ignore the proxy of the Windows settings). A password in `https_proxy` (`user:password@`) is masked in `GET /api/v1/settings` and `desk config`; the page sending the masked value back keeps the stored one.

| Variable | Default | Meaning |
|---|---|---|
| `DESK_HUB_URL` | not set | The VPS hub, set per installation (the address is not kept in the repository); HTTPS only |
| `DESK_HUB_TOKEN_FILE` / `DESK_HUB_TOKEN` | `<home>/hub_token` | Bearer token of this consumer, scope `read` (snapshots and the history API; `snapshots` alone: no history API, R24). Keep it in a file outside the repository; the repository is public |
| `DESK_HUB_CA_BUNDLE`, `HTTPS_PROXY` | system | Corporate proxy and its TLS-inspection CA (inside the work network). The client uses the standard library with the OS verifier through `truststore` (on Windows the certificate store, including roots Windows downloads on demand and those a group policy installs; else the `ssl` default). `DESK_HUB_CA_BUNDLE` replaces it |
| `DESK_HUB_TIMEOUT_S` | 60 | Per-request timeout; 5xx and connection errors are retried three times with backoff |
| `DESK_SNAPSHOT_CACHE` | `<home>/snapshots` | Verified bundles, append-only; `desk sync` writes here, `desk eod` and `desk demo-book` read here unless `--snapshots` is given |
| `DESK_SNAPSHOT_DROP` | not set | Folder an operator fills by hand when the hub is unreachable (`desk import-drop`) |
| `DESK_STORE_URL` | not set | The desk store, a `postgresql://` URI (ADR-006). Unset: the embedded server in `<home>/pg` (in the dev checkout `.devdata/desk_pg`, never the hub's `.devdata/pg`) |
| `DESK_REPORT_DIR` | not set | Validation reports (`--report-dir`) |
| `DESK_PRIVATE_ADAPTER` | not set | The private adapter (08 §2): a package directory (imported by path, not installed) or a module name. Unset: the reference adapter `desk.inbox.reference` (canonical CSV) |
| `DESK_PRIVATE_ADAPTER_CONFIG` | `<home>/adapter-config` | The adapter's local configuration folder (`ctx.config_dir`, created on demand); part of the fingerprint |
| `DESK_PRIVATE_ADAPTER_SHA256` | not set | The expected adapter fingerprint; loading fails (ingestion off) on a mismatch |
| `DESK_INBOX_DIRS` | not set | Report folders, separated by `;`; one ending in `\**` (or `/**`) is scanned recursively |
| `DESK_INBOX_ARCHIVE` | `<home>/inbox-archive` | A copy of every matched file, under `<sha256>/<name>`, for `retry` and `replay` |
| `DESK_INBOX_POLL_SECONDS` | 60 | Watcher period; a file is offered when unchanged since the previous scan or older than this |
| `DESK_ADAPTER_MATCH_TIMEOUT_S` / `DESK_ADAPTER_READ_TIMEOUT_S` | 60 / 600 | Per batch of up to 50 `match` calls (the workers' start-up apart: under the read timeout) / per `read` |
| `DESK_ADAPTER_CHECK_TIMEOUT_S` | 60 | One `reference()` run when a settings form checks a file with the adapter (`adapter_check_timeout_s` in `settings.json`). The current files' run is cached until the adapter or its config folder changes (10 min at most); both runs go to one warm child process. A save never waits for the check ("сохранить без проверки") |
| `DESK_INBOX_MAX_FILE_MB` | 500 | Larger files are not offered |
| `DESK_AUTOPILOT` / `DESK_AUTOPILOT_SYNC_LAST` | on / 5 | The `desk serve` autopilot and the number of latest hub dates it keeps in sync |
| `DESK_INTRADAY` | on | The autopilot's intraday step: today's book on the hub's intraday cuts every half hour, from 19:45 on the preliminary close |
| `DESK_HISTORY_DAYS` | 260 | Hub business dates of market history for the bond charts (about a year), fetched by the autopilot a few per cycle, when the hub has no history API; 0: the history step off (the cached dates only) |
| `DESK_HISTORY_BONDS_FROM` / `DESK_HISTORY_RATES_FROM` | 2024-01-03 / 2010-01-01 | First date read from the hub's history API: bonds / the KBD, curves, fixings and FX (R24) |
| `DESK_BACKFILL` / `DESK_BACKFILL_FROM` / `DESK_BACKFILL_BUDGET_S` | on / 2026-01-05 / 600 | The autopilot's backfill step (R23): the default runs of the past business dates with positions from this date, at most this many seconds a cycle (one date always finishes) |
| `DESK_HOME` | see above | The per-user data folder (environment or env file only) |
| `DESK_STATIC_ROOT` | the repository root | Where `desk serve` finds `prototypes/` and `frontend/dist/` (process environment only); the Windows bundle's `start.bat` sets it to its `app\` folder |
| `DESK_DEFAULT_PRIVATE_ADAPTER` | not set | The lowest-priority adapter (process environment only): `start.bat` sets it to the package in the bundle's `adapter\` folder |

## Tests

```bash
cd backend && .venv/bin/pytest -q
```

The tests run slices S1–S3 on the hub's golden fixtures, vendored in `tests/fixtures/mdhub_v1/` (ten public bonds of 2026-10-15). The fixtures hold one date, so the S2 tests build the previous date by shifting the KBD parameters and the RUONIA OIS and key-rate curves. The S3 tests serve the same fixtures from an in-process stub hub on `127.0.0.1` (paging, 401/403/404, retries, corrupt bodies, the drop folder, the token never printed). The S4 tests cover the core mapping (`test_slice_s4.py`), floaters, perpetuals and calendars (`test_slice_s4_floaters.py`), the RUONIA OIS and key-rate curves (`test_curves_rates.py`), the fallback rungs (`test_fallback.py`) and the flash PnL against core's `explain_horizon_pnl` (`test_pnl_compare.py`). The S5 tests run on an embedded PostgreSQL per test session (`pgserver`, a fresh migrated database per test): the bitemporal positions and immutability triggers (`test_store.py`), a run published into the store (`test_slice_s5.py`), the carried model spread (`test_model_spread.py`), and the desk API with its `window.DESK` adapter (`test_api.py`). The S6 tests cover the scenario definition and hash, the Taylor against core and the exact repricing of a golden run (`test_slice_s6.py`, which also checks the committed parity fixture `tests/fixtures/taylor_parity.json`; `DESK_REGEN_FIXTURES=1` regenerates it), and the scenario store, jobs, worker and routes with the committed `frontend/openapi.json` (`test_scenario_api.py`). `test_intraday.py` covers the intraday mode on the stub hub: a cut picked up, no hub question within a 10-minute slot, an old date whose sync fails not blocking the run (#20) and no run without a newer cut, one intraday run per day, the preliminary close in the evening, the final run the next morning, the PnL basis label (an earlier final, a preliminary close), the setting off, the cache prune, the morning run of the book on the previous close without the hub, made once and replaced by the first cut (#21). `test_market_history.py` covers the market history: the tables filled from the cache once, corrections, the history route over 31 dates (the fixture date shifted, written as bundles of their own) with the KBD rate, and the autopilot's history step and its budget. `test_hub_history.py` covers the history API (R24) on the stub `hub_history_stub.py`: the client (paging, gzip, ETag and 304, `Decimal` digits, retries, 400/401/403/404), the sync (full, then nothing to do, tails and chunks revalidated by 304s, a query resumed across cycles within the budget, a corrected past day and a dropped row, the desk's bonds first), the fallback to the snapshots with its reason for a hub before 0.2.0 and a token without `read`, and the readers (bond and positions history from 2024-01-03, Z-spread, DM, linkers, the market endpoint). `test_r23_backfill.py` covers the history of runs (the plan, carried positions, oldest first, a prepared market reused, skipped unchanged dates, a logic change, failures, the budget, the autopilot step, the older reports, the CLI, the status API) and `test_r23_concepts.py` the static facts, the market calendar and its drill-down, the CBR meetings, the run summaries, PnL periods and the cached bodies. Snapshots of the whole universe are local only.

`pytest -m live` checks the real hub in `DESK_HUB_URL` (hub-check and a sync of its latest date); the default run never touches the network.

## Layout

| Module | Role | Architecture |
|---|---|---|
| `marketdata/hub.py` | HTTP client of the hub's `/v1/snapshots` and `/v1/history/*` APIs (gzip, ETag / 304, `Decimal`); the only module that knows the hub's paths | ADR-001 §4 |
| `marketdata/cache.py` | `sync` and `import_drop`: verified, atomic, append-only writes into the cache | ADR-001 §4 |
| `marketdata/history.py` | Market history for the bond charts: daily quotes, KBD and bond terms of the snapshots seen (`md_*` tables), the series of `GET /api/v1/bonds/history` | R7 |
| `marketdata/hubhistory.py` | Deep history from the hub's history API (`md_hub_*`): jobs per month / year and the tail, ETag revalidation, resumable paging within a budget, the fallback's reason | R24 |
| `marketdata/bundle.py` | Reads `<snapshot_id>.json.gz` bundles, verifies sha256 | ADR-002 |
| `marketdata/adapter_rates.py` | Contract v1 rates inputs: swap quote sets, fixings, CBR meetings, bounded calendars extended provisionally to 2079 (merged into `adapter.py` later) | ADR-003 |
| `curves/rates.py` | RUONIA OIS and key-rate projection curves, point-in-time fixings, through core; quote-set choice in the lineage | ADR-004 |
| `marketdata/adapter.py` | Contract v1 → `BondTerms`, `BondQuote`, and core `Instrument`, `BondPriceObservation`, `MarketSnapshot` (KBD, calendars); the only module that knows the wire format | ADR-003 |
| `valuation/bonds.py` | Eligibility with reason codes, put/call horizon (F2), coupons known to it, price basis; one core `value_portfolio` call per spread kind at the trade date | 04 §1, 01 §3 |
| `valuation/fallback.py` | The ladder below core: market value, stale price, model price, par proxy, each with its quality flag; the simple PnL for those rungs | 01 §3, the owner's rule |
| `pnl/compare.py` | Regression check: the flash PnL against core `explain_horizon_pnl` | VERTICAL_SLICES S4 |
| `pnl/flash.py` | Day-over-day flash PnL by exact repricing: carry, rolldown, rates, spread, other, with the identity check | VERTICAL_SLICES S2 |
| `pnl/direct.py` | PnL without core: FX rows and a foreign bond's FX part by the rate move, report-valued rows by the reports (R10) | R10 |
| `pnl/reasons.py` | No-PnL reason codes and PnL methods with their Russian text, for the run log and `window.DESK` | R10 |
| `orchestrator.py` | One EOD date: prepare snapshots and curve, value requests, attach the flash PnL | 04 §1 |
| `store/` | The desk store: migrations, bitemporal positions (`load_positions`, `positions_for`), carried spreads, orphan reaping | ADR-006, 04 §2 |
| `publish/desk_run.py` | Writes an immutable run with its lineage, core results, model spreads, coverage and totals | 04 §3 |
| `publish/tdm_run.py` | The run content (`run_content`: rows, reference, log) that `desk_run` publishes; the name is historical (the tdm SQLite writer is gone, #294) | 05 §4 |
| `api/` | Desk API v1 (FastAPI): runs, default run, positions, PnL, aggregates, risk, scenario definitions and jobs, health; the `window.DESK` adapter (with `SCENARIOS` from the store), the prototypes and the React app at `/app/` | 05 §2, §4 |
| `scenario/` | Scenarios: the definition, its normalized form and hash (`definition.py`); the run's risk payload and the Taylor sum (`taylor.py`, the reference for the browser's port); exact repricing of a run through core (`exact.py`); the job store and worker (`jobs.py`) | 03 T1/T2, VERTICAL_SLICES S6 |
| `report/validation.py` | Universe-wide comparison with MOEX analytics | VERTICAL_SLICES S1 |
