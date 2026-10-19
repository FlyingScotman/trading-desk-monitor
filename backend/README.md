# Desk backend (`desk`)

The Trading Desk Monitor backend. Design: [`docs/architecture/04_BACKEND.md`](../docs/architecture/04_BACKEND.md). Current state: **slices S1–S6** (next: S7a, the real book through the private adapter; see `docs/architecture/ROADMAP.md` §0) of [`docs/architecture/VERTICAL_SLICES.md`](../docs/architecture/VERTICAL_SLICES.md). It pulls verified snapshots from the VPS hub into a local cache, values every bond of a book through core 0.32.0 (pinned by commit from its GitHub repository) or, where core cannot, by a flagged approximation (see "Valuation quality"), explains the day-over-day flash PnL by exact repricing, and publishes immutable EOD runs into the desk store (PostgreSQL), which the desk API serves to the Desk Monitor prototype and to the Scenario Lab, the first React screen (`frontend/`). A scenario is estimated by Taylor in the browser from the run's stored risk and repriced exactly by core on the run's own snapshot.

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
- The hub's address and a token of scope `snapshots` (see Settings). The tests need neither; they run on vendored fixtures and a local stub hub.
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

`desk runs` lists the store's runs. Serve the desk API and the Desk Monitor on the store:

```bash
cd backend && .venv/bin/desk serve
```

Then open:
- http://127.0.0.1:8000/desk-monitor.html for the Desk Monitor on the latest default run (the picker at the bottom left switches runs); the API is under `/api/v1` (`/docs` for the OpenAPI);
- http://127.0.0.1:8000/app/ for the Scenario Lab (slice S6): pick a run and a preset or move the sliders, read the Taylor estimate (≈), then "Посчитать точно" for the exact repricing and its residual;
- `.devdata/reports/validation_<date>.html` for the comparison with MOEX analytics over the whole bond universe.

`desk serve` also runs the exact scenario worker, a thread that reprices queued jobs from the run's bundles in `DESK_SNAPSHOT_CACHE` (one worker per store); `desk serve --no-worker` leaves jobs queued. The scenario routes:

| Route | What |
|---|---|
| `GET /api/v1/runs/{id}/risk` | The run's stored risk per position (sparse delta, gamma upper triangle), for the Taylor in the browser (T1) |
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
desk eod --date 2026-10-15         # values the inbox positions; prints each book's source and age
```

`desk inbox retry --status failed,rejected` offers files again; `desk inbox replay --source S --from D1 --to D2` re-reads archived accepted files and stores a new revision only where the rows changed. When the adapter fails to load, ingestion is off with the reason (`desk inbox status`, `/health/ready`), and runs and the API keep working.

### Daily flow (autopilot)

`desk serve` runs the autopilot (`desk/autopilot.py`), a thread that every `DESK_INBOX_POLL_SECONDS` (60), and at once on `POST /api/v1/autopilot/run`, runs one cycle in this order:

1. **sync**: the `autopilot_sync_last` (5) latest hub dates, every business date with positions in the last two weeks and the hub date before it (flash PnL), and a snapshot whose calendar covers each inbox file still waiting for one; cached dates are skipped (asked again once an hour, for corrections). The drop folder, when set, is imported too. A hub error is shown and the cycle goes on with the cache.
2. **scan**: `desk inbox scan`. It comes after the sync because a start-of-day report needs a cached calendar covering its date and the day before (otherwise the file waits, `waiting_calendar`, and is retried next cycle).
3. **eod**: for each cached `eod_final` date of the last two weeks (at least the latest date with positions) that has positions, the `desk eod` run when the date has no default run, or its default run read other position sets (a new revision), another market snapshot or another previous snapshot. Oldest first; a date that failed is retried only when its inputs change.

Settings are read again every cycle. `GET /api/v1/autopilot/status` gives the state, the steps of the last cycle and the latest default run; errors are also logged to `<home>/logs/autopilot.log`. `desk serve --no-autopilot` starts no thread; `--autopilot` runs cycles even when the settings turn it off. `desk cycle` runs one cycle in the foreground and prints it (exit 1 when a step failed; `--date D` also keeps D current, `--no-hub` skips the hub); `windows/daily_backend.bat [hub|drop] [D]` calls it. One cycle at a time per home folder, across processes: a cycle holds `<home>/cycle.lock` (`msvcrt.locking` / `fcntl.flock`, released by the OS if the process dies); a cycle that finds it taken does nothing and reports the step `cycle: skipped` (another cycle running).

### Offline fallback

When the hub is unreachable (the proxy blocks it), an operator copies `<snapshot_id>.json.gz` bundles into `DESK_SNAPSHOT_DROP` and runs `desk import-drop`. Each file is verified like a download; a bad file is rejected with its reason and not copied.

Without any hub, the dev export restores the hub seed into `.devdata/pg` and exports real `eod_final` snapshots for 2026-10-09 … 2026-10-15 to `.devdata/snapshots/` (about 3 minutes). It needs the sibling checkout `../market-data-server` with the hub's release bundle in `dist/` and its snapshot-layer worktree:

```bash
tools/devdata/build_dev_snapshots.sh
```

## Valuation quality

The owner's rule (19 October 2026): a bond is valued somehow rather than not at all. Every valuation carries `quality` and `assumptions`, published in the position's `extra`, the run log and the validation report:

| `quality` | How | Example assumptions |
|---|---|---|
| `exact` | core `value_portfolio` on the published terms | none |
| `assumed` | core, with stated assumptions | floater formula `inferred` with the default convention; perpetual to an assumed call; future coupons at the current rate; accrued computed; accrued mismatch vs MOEX |
| `market_value` | today's price × face + accrued (× FX for a foreign face); risk from core calibrated to the price, else from the duration | the reason core could not value it |
| `stale_price` | the same with the last price of the previous 30 days | price date and age |
| `model_price` | the bond's schedule on the KBD at the Z-spread carried from the latest earlier run in the store (the spread its model price used, or its last market Z if newer), so only the curve moves it; without one, the median Z of its issuer, class or kind (traded bonds, Z within −100…2000 bp) | where the spread comes from and since when |
| `par_proxy` | current face (+ accrued if known), no risk | no price, no usable schedule |

A bond past its final redemption, or fully amortized, is valued 0. The face is the one outstanding on the business date from the redemption schedule. Only a bond with no data at all (no terms, no face) stays without a valuation, and an unexpected error for one bond sends it to `par_proxy` with the error named.

Exact and assumed bonds get the flash PnL by exact repricing. Market value and stale or model price get PnL = q·(dirty_D − dirty_D−1) + received, all in `other`; across a model price it carries a flag ("price discovery: model -> market"). A position at par on either date gets no PnL (reason `par_proxy`).

## Settings

`desk.settings` reads, the first that sets a key winning: `DESK_*` environment variables, then `<home>/settings.json`, then `backend/.env` (or `DESK_ENV_FILE`), then the defaults. `<home>` is the per-user data folder, outside the install folder so that a new version unpacked anywhere keeps the settings and the data: `DESK_HOME`, else `%LOCALAPPDATA%\DeskMonitor` on Windows, else `<repo>/.devdata` in a git checkout (today's dev layout, unchanged), else `~/.local/share/desk-monitor`. It holds `settings.json`, `hub_token`, `adapter-config/`, `snapshots/`, `inbox-archive/`, `pg/` (embedded store), `tdm.sqlite` and `logs/` (the dev checkout keeps `.devdata/inbox` and `.devdata/desk_pg`).

The settings page `http://127.0.0.1:8000/settings.html` (`desk serve`) edits `settings.json` (keys in lower case: `hub_url`, `hub_ca_bundle`, `hub_timeout_s`, `https_proxy`, `no_proxy`, `inbox_dirs` as `[{"path", "label", "recursive"}]`, `private_adapter`, `private_adapter_config`, `inbox_poll_seconds`, `autopilot`, `autopilot_sync_last`, `snapshot_drop`, `store_url`; relative paths against `<home>`), stores the hub token in `<home>/hub_token` (mode 0600; never returned by the API), checks the hub, shows the adapter's status and edits the config files it declares (`describe()["config_files"]`, 08 §4). `load()` re-reads `settings.json` on every call, so the running server picks up edits. Its routes (`/api/v1/settings`, `/api/v1/settings/hub-token`, `/api/v1/settings/test-hub`, `/api/v1/adapter`, `/api/v1/adapter/config/{name}`) answer loopback clients only and are left out of the OpenAPI document. `backend/.env` remains for development: copy [`.env.example`](.env.example) to `.env`; it is git-ignored. `desk config` prints the effective values, with the token masked.

| Variable | Default | Meaning |
|---|---|---|
| `DESK_HUB_URL` | not set | The VPS hub, set per installation (the address is not kept in the repository); HTTPS only |
| `DESK_HUB_TOKEN_FILE` / `DESK_HUB_TOKEN` | `<home>/hub_token` | Bearer token of this consumer, scope `snapshots`. Keep it in a file outside the repository; the repository is public |
| `DESK_HUB_CA_BUNDLE`, `HTTPS_PROXY` | system | Corporate proxy and its TLS-inspection CA (inside the work network). The client uses the standard library, so on Windows the system certificate store is trusted |
| `DESK_HUB_TIMEOUT_S` | 60 | Per-request timeout; 5xx and connection errors are retried three times with backoff |
| `DESK_SNAPSHOT_CACHE` | `<home>/snapshots` | Verified bundles, append-only; `desk sync` writes here, `desk eod` and `desk demo-book` read here unless `--snapshots` is given |
| `DESK_SNAPSHOT_DROP` | not set | Folder an operator fills by hand when the hub is unreachable (`desk import-drop`) |
| `DESK_STORE_URL` | not set | The desk store, a `postgresql://` URI (ADR-006). Unset: the embedded server in `<home>/pg` (in the dev checkout `.devdata/desk_pg`, never the hub's `.devdata/pg`) |
| `DESK_DB` | `<home>/tdm.sqlite` | The tdm SQLite prototype store, written only with `desk eod --tdm` (`tdm serve`, until window.DESK is retired) |
| `DESK_REPORT_DIR` | not set | Validation reports (`--report-dir`) |
| `DESK_PRIVATE_ADAPTER` | not set | The private adapter (08 §2): a package directory (imported by path, not installed) or a module name. Unset: the reference adapter `desk.inbox.reference` (canonical CSV) |
| `DESK_PRIVATE_ADAPTER_CONFIG` | `<home>/adapter-config` | The adapter's local configuration folder (`ctx.config_dir`, created on demand); part of the fingerprint |
| `DESK_PRIVATE_ADAPTER_SHA256` | not set | The expected adapter fingerprint; loading fails (ingestion off) on a mismatch |
| `DESK_INBOX_DIRS` | not set | Report folders, separated by `;`; one ending in `\**` (or `/**`) is scanned recursively |
| `DESK_INBOX_ARCHIVE` | `<home>/inbox-archive` | A copy of every matched file, under `<sha256>/<name>`, for `retry` and `replay` |
| `DESK_INBOX_POLL_SECONDS` | 60 | Watcher period; a file is offered when unchanged since the previous scan or older than this |
| `DESK_ADAPTER_MATCH_TIMEOUT_S` / `DESK_ADAPTER_READ_TIMEOUT_S` | 10 / 600 | Per batch of up to 50 `match` calls / per `read` |
| `DESK_INBOX_MAX_FILE_MB` | 500 | Larger files are not offered |
| `DESK_AUTOPILOT` / `DESK_AUTOPILOT_SYNC_LAST` | on / 5 | The `desk serve` autopilot and the number of latest hub dates it keeps in sync |
| `DESK_HOME` | see above | The per-user data folder (environment or env file only) |

## Tests

```bash
cd backend && .venv/bin/pytest -q
```

The tests run slices S1–S3 on the hub's golden fixtures, vendored in `tests/fixtures/mdhub_v1/` (ten public bonds of 2026-10-15). The fixtures hold one date, so the S2 tests build the previous date by shifting the KBD parameters. The S3 tests serve the same fixtures from an in-process stub hub on `127.0.0.1` (paging, 401/403/404, retries, corrupt bodies, the drop folder, the token never printed). The S4 tests cover the core mapping (`test_slice_s4.py`), floaters, perpetuals and calendars (`test_slice_s4_floaters.py`), the RUONIA OIS and key-rate curves (`test_curves_rates.py`), the fallback rungs (`test_fallback.py`) and the flash PnL against core's `explain_horizon_pnl` (`test_pnl_compare.py`). The S5 tests run on an embedded PostgreSQL per test session (`pgserver`, a fresh migrated database per test): the bitemporal positions and immutability triggers (`test_store.py`), a run published into the store (`test_slice_s5.py`), the carried model spread (`test_model_spread.py`), and the desk API with the parity of its `window.DESK` adapter against `tdm.assemble` (`test_api.py`). The S6 tests cover the scenario definition and hash, the Taylor against core and the exact repricing of a golden run (`test_slice_s6.py`, which also checks the committed parity fixture `tests/fixtures/taylor_parity.json`; `DESK_REGEN_FIXTURES=1` regenerates it), and the scenario store, jobs, worker and routes with the committed `frontend/openapi.json` (`test_scenario_api.py`). Snapshots of the whole universe are local only.

`pytest -m live` checks the real hub in `DESK_HUB_URL` (hub-check and a sync of its latest date); the default run never touches the network.

## Layout

| Module | Role | Architecture |
|---|---|---|
| `marketdata/hub.py` | HTTP client of the hub's `/v1/snapshots` API; the only module that knows the hub's paths | ADR-001 §4 |
| `marketdata/cache.py` | `sync` and `import_drop`: verified, atomic, append-only writes into the cache | ADR-001 §4 |
| `marketdata/bundle.py` | Reads `<snapshot_id>.json.gz` bundles, verifies sha256 | ADR-002 |
| `marketdata/adapter_rates.py` | Contract v1 rates inputs: swap quote sets, fixings, CBR meetings, bounded calendars extended provisionally to 2079 (merged into `adapter.py` later) | ADR-003 |
| `curves/rates.py` | RUONIA OIS and key-rate projection curves, point-in-time fixings, through core; quote-set choice in the lineage | ADR-004 |
| `marketdata/adapter.py` | Contract v1 → `BondTerms`, `BondQuote`, and core `Instrument`, `BondPriceObservation`, `MarketSnapshot` (KBD, calendars); the only module that knows the wire format | ADR-003 |
| `valuation/bonds.py` | Eligibility with reason codes, put/call horizon (F2), coupons known to it, price basis; one core `value_portfolio` call per spread kind at the trade date | 04 §1, 01 §3 |
| `valuation/fallback.py` | The ladder below core: market value, stale price, model price, par proxy, each with its quality flag; the simple PnL for those rungs | 01 §3, the owner's rule |
| `pnl/compare.py` | Regression check: the flash PnL against core `explain_horizon_pnl` | VERTICAL_SLICES S4 |
| `pnl/flash.py` | Day-over-day flash PnL by exact repricing: carry, rolldown, rates, spread, other, with the identity check | VERTICAL_SLICES S2 |
| `orchestrator.py` | One EOD date: prepare snapshots and curve, value requests, attach the flash PnL | 04 §1 |
| `store/` | The desk store: migrations, bitemporal positions (`load_positions`, `positions_for`), carried spreads, orphan reaping | ADR-006, 04 §2 |
| `publish/desk_run.py` | Writes an immutable run with its lineage, core results, model spreads, coverage and totals | 04 §3 |
| `publish/tdm_run.py` | The run content both stores share; writes it through `tdm.writer.Run` with `--tdm` (transition) | 05 §4 |
| `api/` | Desk API v1 (FastAPI): runs, default run, positions, PnL, aggregates, risk, scenario definitions and jobs, health; the `window.DESK` adapter (with `SCENARIOS` from the store), the prototypes and the React app at `/app/` | 05 §2, §4 |
| `scenario/` | Scenarios: the definition, its normalized form and hash (`definition.py`); the run's risk payload and the Taylor sum (`taylor.py`, the reference for the browser's port); exact repricing of a run through core (`exact.py`); the job store and worker (`jobs.py`) | 03 T1/T2, VERTICAL_SLICES S6 |
| `report/validation.py` | Universe-wide comparison with MOEX analytics | VERTICAL_SLICES S1 |
