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
- http://127.0.0.1:8100/desk-monitor.html for the Desk Monitor on the latest default run (the picker at the bottom left switches runs); the API is under `/api/v1` (`/docs` for the OpenAPI);
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
desk eod --date 2026-10-15         # values the inbox positions; prints each book's source and age
```

`desk inbox retry --status failed,rejected` offers files again; `desk inbox replay --source S --from D1 --to D2` re-reads archived accepted files and stores a new revision only where the rows changed. When the adapter fails to load, ingestion is off with the reason (`desk inbox status`, `/health/ready`), and runs and the API keep working.

### Daily flow (autopilot)

`desk serve` runs the autopilot (`desk/autopilot.py`), a thread that every `DESK_INBOX_POLL_SECONDS` (60), and at once on `POST /api/v1/autopilot/run`, runs one cycle in this order:

1. **sync**: the `autopilot_sync_last` (5) latest hub dates, every business date with positions in the last two weeks and the hub date before it (flash PnL), and a snapshot whose calendar covers each inbox file still waiting for one; cached dates are skipped (asked again once an hour, for corrections). Never a date older than the inbox lookback (`inbox_lookback_days`, 10; the hub date before its first day apart), at most `autopilot_sync_last` + 3 dates per cycle, and a date the hub has no snapshot for is not asked again for 6 hours. The drop folder, when set, is imported too. A hub error is shown and the cycle goes on with the cache. Then every bundle of the cache not loaded yet goes into the store's market history (see "Market history"; a cycle with nothing new costs one query).
2. **scan**: `desk inbox scan`. It comes after the sync because a start-of-day report needs a cached calendar covering its date and the day before (otherwise the file waits, `waiting_calendar`, and is retried next cycle). Within the lookback only: files modified earlier are skipped on a `stat` (never hashed), files whose report date is older are ledgered once as `ignored_too_old` and never read, and waiting files whose report date falls behind get that status too; a folder of years of reports costs a directory listing. A backfill of older dates (S8) will take its own explicit date range.
3. **eod**: for each cached `eod_final` date of the last two weeks (at least the latest date with positions) that has positions, the `desk eod` run when the date has no default run, or its default run read other position sets (a new revision), another market snapshot or another previous snapshot; the latest date with positions also when its default run was made by another program version (`desk.logic_version()`: backend version, the hand-bumped `VALUATION_VERSION`, core version and, in the Windows bundle, its `VERSION`), so an upgrade recomputes it once. Oldest first; a date that failed is retried only when its inputs change.
4. **intraday** (with a hub and `intraday` on, the default; R11): on a business day from 10:00 MSK, today's book (the latest position sets, carried forward) valued on the hub's newest `intraday` cut of today, and from 19:45 on its `eod_preliminary` cut: the `desk eod` code path on another market snapshot, an `INTRADAY` run (`cut_type` intraday; a preliminary one is an EOD run, the date's default until the final). The hub is asked (one list call, `latest=true`) at most once per half-hour slot (:05 and :35), and for the preliminary cut only until today has a run on it; a run is made only for a cut newer than the last run's, or when the position sets changed. The flash PnL is against the previous business day's `eod_final`; when the cache lacks it, against the newest earlier `eod_final`, else the newest earlier `eod_preliminary`, and the run says so (`lineage.pnl_basis`, `pnl_basis` in the API: «PnL от 15.10 (закрытие): нет закрытия 16.10»); the EOD runs follow the same rule. Only the latest `INTRADAY` run of a date is kept: the older ones are deleted with their rows once a newer one is complete (migration 0010 lets only those be deleted; a run a scenario job was submitted on stays). Intraday and preliminary bundles of dates older than 3 days leave the snapshot cache, unless a run still shown reads them. The next morning the date's `eod_final` run (step 3) supersedes both. Runs record `cut_ts` (the cut's nominal instant) and `prev_cut_type`.
5. **history** (with a hub and `history_days` > 0): the market history beyond the cache for the bond charts, 5 of the `history_days` (260, about a year) latest hub dates per cycle, newest first, straight into the store without caching the bundles. Only after a sync that reached the hub and left no date for the next cycle, and after the EOD runs; the inbox lookback does not apply (these dates feed no run). The hub's date list is read once an hour.

The run pages open on is the newest run shown (`store.CURRENT_RUNS`): per date its default run, otherwise its intraday run, so during the day today's intraday run. `GET /api/v1/runs/default` without a date returns it, `GET /api/v1/runs` marks it `is_current`, and every run header carries its slice (`cut_type`, `cut_ts`, `label`: «20.10 14:30 МСК · внутри дня», «19.10 · закрытие предв.», «19.10 · закрытие») and `pnl_basis`; `window.DESK.source` has `cutType`, `cutTs`, `slice`, `prevCutType` and `pnlBasis`, and the shared nav bar names runs by their slice.

Settings are read again every cycle. `GET /api/v1/autopilot/status` gives the state, the steps of the last cycle and the latest run shown with its slice; errors are also logged to `<home>/logs/autopilot.log`. `desk serve --no-autopilot` starts no thread; `--autopilot` runs cycles even when the settings turn it off. `desk cycle` runs one cycle in the foreground and prints it (exit 1 when a step failed; `--date D` also keeps D current, `--no-hub` skips the hub); the Windows bundle's `update_daily.bat` calls it (its arguments go to `desk cycle`). One cycle at a time per home folder, across processes: a cycle holds `<home>/cycle.lock` (`msvcrt.locking` / `fcntl.flock`, released by the OS if the process dies); a cycle that finds it taken does nothing and reports the step `cycle: skipped` (another cycle running).

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

## Valuation quality

The owner's rule (19 October 2026): a bond is valued somehow rather than not at all. Every valuation carries `quality` and `assumptions`, published in the position's `extra`, the run log and the validation report:

| `quality` | How | Example assumptions |
|---|---|---|
| `exact` | core `value_portfolio` on the published terms | none |
| `assumed` | core, with stated assumptions | floater formula `inferred` with the default convention; perpetual to an assumed call; future coupons at the current rate; accrued computed; accrued mismatch vs MOEX |
| `market_value` | today's price × face + accrued (× FX for a foreign face); risk from core calibrated to the price, else from the duration (a foreign bond's always, on its currency's curve) | the reason core could not value it; a duration proxy (foreign face, OFZ-IN indexed face) |
| `stale_price` | the same with the last price of the previous 30 days | price date and age |
| `model_price` | the bond's schedule on the KBD at the Z-spread carried from the latest earlier run in the store (the spread its model price used, or its last market Z if newer), so only the curve moves it; without one, the median Z of its issuer, class or kind (traded bonds, Z within −100…2000 bp) | where the spread comes from and since when |
| `par_proxy` | current face (+ accrued if known), no risk | no price, no usable schedule |

A bond past its final redemption, or fully amortized, is valued 0. The face is the one outstanding on the business date from the redemption schedule. Only a bond with no data at all (no terms, no face) stays without a valuation, and an unexpected error for one bond sends it to `par_proxy` with the error named.

### Risk by curve

Bucket DV01 and the Hessian are published per curve (owner request R8, phase 1); `dv01` is their sum:

| Position | Curves of its risk |
|---|---|
| Fixed RUB bond (core, or a fallback rung on the KBD) | `RUB OFZ` (the KBD) |
| Floater (OFZ-PK on RUONIA, corporate on the key rate or RUONIA) | `RUB OFZ` (discounting) and `RUB RUONIA` or `RUB KEY` (projection): two legs of opposite sign, the basis, with the cross terms in the Hessian |
| Foreign bond (fallback, core has no curve in its currency) | `USD`, `EUR`, `CNY`…: DV01 in RUB from the MOEX duration, a proxy |
| OFZ-IN (fallback) | `RUB OFZ`, nominal DV01 from the duration, flagged as a proxy (no real curve or inflation factor yet) |
| Report's own risk (`risk_dv01_rub`: unmapped bond, OTC) | the curve of the row's currency (`RUB OFZ` for RUB) |

The run publishes `RUB OFZ`, `RUB RUONIA` and `RUB KEY` with their points (KBD effective yields; zero rates of the RUONIA OIS and key-rate projection curves, annual compounding) and the day's move, and a curve without points for a currency's curve. A scenario shifts each curve by its own vector (`desk.scenario.exact.core_scenario`); the preset moves of all rates shift the three RUB curves alike (store migration 0008).

Exact and assumed bonds get the flash PnL by exact repricing. Market value and stale or model price get PnL = q·(dirty_D − dirty_D−1) + received, all in `other`; across a model price it carries a flag ("price discovery: model -> market"). A position at par on either date gets no PnL (reason `par_proxy`). A foreign-face bond's FX revaluation (its FX delta of D−1 × the rate move) is taken out of `other` into `fx_gross`. Rows valued without core (`pnl/direct.py`): an FX row gets amount × (rate D − rate D−1), method `desk-fx-spot-v1` (a forward only its spot delta, flagged); a row at the report's value (OTC, unmapped bonds) gets today's reported PV − yesterday's for the same position, method `desk-reported-v1`, flag «по отчёту», shown as its own explain step; none when yesterday had no reported value or the book's report was not renewed.

A missing PnL is never 0 (R10): a position without one has `run_position.no_pnl` {reason, detail}, and the `window.DESK` adapter gives it null amounts with the reason's Russian text (`pnl/reasons.py`); a run without a previous `eod_final` market has `prevDate` null, `pnlAvailable` false with its reason, and a WARN line in its own log. The Desk Monitor shows «—» with the reason as a tooltip, and totals over the positions with PnL marked «частично: N из M позиций».

## Settings

`desk.settings` reads, the first that sets a key winning: `DESK_*` environment variables, then `<home>/settings.json`, then `backend/.env` (or `DESK_ENV_FILE`), then the defaults. `<home>` is the per-user data folder, outside the install folder so that a new version unpacked anywhere keeps the settings and the data: `DESK_HOME`, else `%LOCALAPPDATA%\DeskMonitor` on Windows, else `<repo>/.devdata` in a git checkout (today's dev layout, unchanged), else `~/.local/share/desk-monitor`. It holds `settings.json`, `hub_token`, `adapter-config/`, `snapshots/`, `inbox-archive/`, `pg/` (embedded store), `tdm.sqlite` and `logs/` (the dev checkout keeps `.devdata/inbox` and `.devdata/desk_pg`).

The settings page `http://127.0.0.1:8100/settings.html` (`desk serve`) edits `settings.json` (keys in lower case: `hub_url`, `hub_ca_bundle`, `hub_timeout_s`, `https_proxy`, `no_proxy`, `inbox_dirs` as `[{"path", "label", "recursive"}]`, `private_adapter`, `private_adapter_config`, `inbox_poll_seconds`, `autopilot`, `autopilot_sync_last`, `intraday`, `inbox_lookback_days`, `history_days`, `snapshot_drop`, `store_url`, `update_dir`; relative paths against `<home>`), stores the hub token in `<home>/hub_token` (mode 0600; never returned by the API), checks the hub, shows the adapter's status and edits the config files it declares (`describe()["config_files"]`, 08 §4). `load()` re-reads `settings.json` on every call, so the running server picks up edits. Its routes (`/api/v1/settings`, `/api/v1/settings/hub-token`, `/api/v1/settings/test-hub`, `/api/v1/adapter`, `/api/v1/adapter/config/{name}`) answer loopback clients only and are left out of the OpenAPI document. `backend/.env` remains for development: copy [`.env.example`](.env.example) to `.env`; it is git-ignored. `desk config` prints the effective values, with the token masked. The proxy keys go into the process environment unless it sets them already: `https_proxy` as `HTTPS_PROXY`, `no_proxy` as `NO_PROXY` only when an HTTPS proxy is set too (on Windows any `*_PROXY` variable, `NO_PROXY` alone included, makes Python ignore the proxy of the Windows settings). A password in `https_proxy` (`user:password@`) is masked in `GET /api/v1/settings` and `desk config`; the page sending the masked value back keeps the stored one.

| Variable | Default | Meaning |
|---|---|---|
| `DESK_HUB_URL` | not set | The VPS hub, set per installation (the address is not kept in the repository); HTTPS only |
| `DESK_HUB_TOKEN_FILE` / `DESK_HUB_TOKEN` | `<home>/hub_token` | Bearer token of this consumer, scope `snapshots`. Keep it in a file outside the repository; the repository is public |
| `DESK_HUB_CA_BUNDLE`, `HTTPS_PROXY` | system | Corporate proxy and its TLS-inspection CA (inside the work network). The client uses the standard library with the OS verifier through `truststore` (on Windows the certificate store, including roots Windows downloads on demand and those a group policy installs; else the `ssl` default). `DESK_HUB_CA_BUNDLE` replaces it |
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
| `DESK_INTRADAY` | on | The autopilot's intraday step: today's book on the hub's intraday cuts every half hour, from 19:45 on the preliminary close |
| `DESK_HISTORY_DAYS` | 260 | Hub business dates of market history for the bond charts (about a year), fetched by the autopilot a few per cycle; 0: the cached dates only |
| `DESK_HOME` | see above | The per-user data folder (environment or env file only) |
| `DESK_STATIC_ROOT` | the repository root | Where `desk serve` finds `prototypes/` and `frontend/dist/` (process environment only); the Windows bundle's `start.bat` sets it to its `app\` folder |
| `DESK_DEFAULT_PRIVATE_ADAPTER` | not set | The lowest-priority adapter (process environment only): `start.bat` sets it to the package in the bundle's `adapter\` folder |

## Tests

```bash
cd backend && .venv/bin/pytest -q
```

The tests run slices S1–S3 on the hub's golden fixtures, vendored in `tests/fixtures/mdhub_v1/` (ten public bonds of 2026-10-15). The fixtures hold one date, so the S2 tests build the previous date by shifting the KBD parameters. The S3 tests serve the same fixtures from an in-process stub hub on `127.0.0.1` (paging, 401/403/404, retries, corrupt bodies, the drop folder, the token never printed). The S4 tests cover the core mapping (`test_slice_s4.py`), floaters, perpetuals and calendars (`test_slice_s4_floaters.py`), the RUONIA OIS and key-rate curves (`test_curves_rates.py`), the fallback rungs (`test_fallback.py`) and the flash PnL against core's `explain_horizon_pnl` (`test_pnl_compare.py`). The S5 tests run on an embedded PostgreSQL per test session (`pgserver`, a fresh migrated database per test): the bitemporal positions and immutability triggers (`test_store.py`), a run published into the store (`test_slice_s5.py`), the carried model spread (`test_model_spread.py`), and the desk API with the parity of its `window.DESK` adapter against `tdm.assemble` (`test_api.py`). The S6 tests cover the scenario definition and hash, the Taylor against core and the exact repricing of a golden run (`test_slice_s6.py`, which also checks the committed parity fixture `tests/fixtures/taylor_parity.json`; `DESK_REGEN_FIXTURES=1` regenerates it), and the scenario store, jobs, worker and routes with the committed `frontend/openapi.json` (`test_scenario_api.py`). `test_intraday.py` covers the intraday mode on the stub hub: a cut picked up, no hub question within a half-hour slot and no run without a newer cut, one intraday run per day, the preliminary close in the evening, the final run the next morning, the PnL basis label (an earlier final, a preliminary close), the setting off, the cache prune. `test_market_history.py` covers the market history: the tables filled from the cache once, corrections, the history route over 31 dates (the fixture date shifted, written as bundles of their own) with the KBD rate, and the autopilot's history step and its budget. Snapshots of the whole universe are local only.

`pytest -m live` checks the real hub in `DESK_HUB_URL` (hub-check and a sync of its latest date); the default run never touches the network.

## Layout

| Module | Role | Architecture |
|---|---|---|
| `marketdata/hub.py` | HTTP client of the hub's `/v1/snapshots` API; the only module that knows the hub's paths | ADR-001 §4 |
| `marketdata/cache.py` | `sync` and `import_drop`: verified, atomic, append-only writes into the cache | ADR-001 §4 |
| `marketdata/history.py` | Market history for the bond charts: daily quotes, KBD and bond terms of the snapshots seen (`md_*` tables), the series of `GET /api/v1/bonds/history` | R7 |
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
| `publish/tdm_run.py` | The run content both stores share; writes it through `tdm.writer.Run` with `--tdm` (transition) | 05 §4 |
| `api/` | Desk API v1 (FastAPI): runs, default run, positions, PnL, aggregates, risk, scenario definitions and jobs, health; the `window.DESK` adapter (with `SCENARIOS` from the store), the prototypes and the React app at `/app/` | 05 §2, §4 |
| `scenario/` | Scenarios: the definition, its normalized form and hash (`definition.py`); the run's risk payload and the Taylor sum (`taylor.py`, the reference for the browser's port); exact repricing of a run through core (`exact.py`); the job store and worker (`jobs.py`) | 03 T1/T2, VERTICAL_SLICES S6 |
| `report/validation.py` | Universe-wide comparison with MOEX analytics | VERTICAL_SLICES S1 |
