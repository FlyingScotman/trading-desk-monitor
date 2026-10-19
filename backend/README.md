# Desk backend (`desk`)

The Trading Desk Monitor backend. Design: [`docs/architecture/04_BACKEND.md`](../docs/architecture/04_BACKEND.md). Current state: **slices S1–S4** (next: S5, the desk store and API) of [`docs/architecture/VERTICAL_SLICES.md`](../docs/architecture/VERTICAL_SLICES.md). It pulls verified snapshots from the VPS hub into a local cache, values every bond of a book through core 0.32.0 (pinned by commit from its GitHub repository) or, where core cannot, by a flagged approximation (see "Valuation quality"), explains the day-over-day flash PnL by exact repricing, and publishes EOD runs that the Desk Monitor prototype shows.

```
VPS hub ──► desk.marketdata.hub + cache (desk sync) ──► snapshot cache (or a hand-filled drop folder)
                          │
hub snapshot files ──► desk.marketdata (bundle + adapter mdhub_v1)
                          │
                          ├─► core Instrument, MarketSnapshot: KBD rub_ofz_kbd_v1, RUONIA OIS rub_ruonia_ois_moex_v2,
                          │   key-rate projection rub_key_rate_projection_v1, fixings (desk.curves.rates)
                          ▼
                       desk.valuation.bonds ──► core value_portfolio (fixed, floaters, perpetuals to an assumed call)
                          │                     hash-verified result in <dir of --db>/results/
                          ├─► desk.valuation.fallback ──► market value, stale or model price, par proxy (flagged)
                          │
                          ├─► desk.pnl.flash ──► previous date revalued, PnL by carry / rolldown / rates / spread
                          │
                          ├─► desk.publish.tdm_run ──► tdm SQLite store ──► tdm serve ──► Desk Monitor
                          └─► desk.report.validation ──► HTML/CSV: our analytics vs MOEX
```

## Run it locally

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

Run EOD for each date, oldest first:

```bash
cd backend && .venv/bin/desk eod --date 2026-10-15 --book examples/test_book.csv --report-dir ../.devdata/reports
```

```bash
backend/.venv/bin/python -m tdm --db .devdata/tdm.sqlite serve --host 127.0.0.1 --port 8765
```

Then open:
- http://127.0.0.1:8765/desk-monitor.html for the Desk Monitor on the real run;
- `.devdata/reports/validation_<date>.html` for the comparison with MOEX analytics over the whole bond universe.

`desk eod` reads only the cache, so a run never depends on the hub being up; `desk eod --sync` pulls the date and the business date before it first. Every run prints the market snapshot ID, where it came from, its `published_at` and age, and records them in the run log lineage. When the cache holds an earlier date, `desk eod` attaches the flash PnL against it (slice S2; `--no-pnl` turns it off).

Books, all synthetic (real public instruments, invented quantities; real desk positions never enter the repository):
- `examples/test_book.csv`: the S3 test book. Three books (`OFZ`, `CORP`, `MIXED`), long and short, with the coverage cases (OFZ-PK, a key-rate floater, OFZ-IN, a perpetual, a bond without quotes on some dates), each valued with its quality flag. Regenerate it with `desk demo-book --profile test --date D --out examples/test_book.csv`;
- `examples/demo_book.csv`: the S1 demo book (`desk demo-book`).

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
| `model_price` | the bond's schedule on the KBD at the median Z of its issuer, class or kind (traded bonds, Z within −100…2000 bp) | the median used |
| `par_proxy` | current face (+ accrued if known), no risk | no price, no usable schedule |

A bond past its final redemption, or fully amortized, is valued 0. The face is the one outstanding on the business date from the redemption schedule. Only a bond with no data at all (no terms, no face) stays without a valuation, and an unexpected error for one bond sends it to `par_proxy` with the error named.

Exact and assumed bonds get the flash PnL by exact repricing. Market value and stale or model price get PnL = q·(dirty_D − dirty_D−1) + received, all in `other`; across a model price it carries a flag ("price discovery: model -> market"). A position at par on either date gets no PnL (reason `par_proxy`).

## Settings

`desk.settings` reads `DESK_*` environment variables, then `backend/.env`. Copy [`.env.example`](.env.example) to `.env`; it is git-ignored. `desk config` prints the effective values, with the token masked.

| Variable | Default | Meaning |
|---|---|---|
| `DESK_HUB_URL` | not set | The VPS hub, set per installation (the address is not kept in the repository); HTTPS only |
| `DESK_HUB_TOKEN_FILE` / `DESK_HUB_TOKEN` | not set | Bearer token of this consumer, scope `snapshots`. Keep it in a file outside the repository; the repository is public |
| `DESK_HUB_CA_BUNDLE`, `HTTPS_PROXY` | system | Corporate proxy and its TLS-inspection CA (inside the work network). The client uses the standard library, so on Windows the system certificate store is trusted |
| `DESK_HUB_TIMEOUT_S` | 60 | Per-request timeout; 5xx and connection errors are retried three times with backoff |
| `DESK_SNAPSHOT_CACHE` | `.devdata/snapshots` | Verified bundles, append-only; `desk sync` writes here, `desk eod` and `desk demo-book` read here unless `--snapshots` is given |
| `DESK_SNAPSHOT_DROP` | not set | Folder an operator fills by hand when the hub is unreachable (`desk import-drop`) |
| `DESK_DB` | `.devdata/tdm.sqlite` | Run store (`--db`) |
| `DESK_REPORT_DIR` | not set | Validation reports (`--report-dir`) |

## Tests

```bash
cd backend && .venv/bin/pytest -q
```

The tests run slices S1–S3 on the hub's golden fixtures, vendored in `tests/fixtures/mdhub_v1/` (ten public bonds of 2026-10-15). The fixtures hold one date, so the S2 tests build the previous date by shifting the KBD parameters. The S3 tests serve the same fixtures from an in-process stub hub on `127.0.0.1` (paging, 401/403/404, retries, corrupt bodies, the drop folder, the token never printed). The S4 tests cover the core mapping (`test_slice_s4.py`), floaters, perpetuals and calendars (`test_slice_s4_floaters.py`), the RUONIA OIS and key-rate curves (`test_curves_rates.py`), the fallback rungs (`test_fallback.py`) and the flash PnL against core's `explain_horizon_pnl` (`test_pnl_compare.py`). Snapshots of the whole universe are local only.

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
| `publish/tdm_run.py` | Writes the run through `tdm.writer.Run` (**slice shortcut** for the desk store) | ADR-006 later |
| `report/validation.py` | Universe-wide comparison with MOEX analytics | VERTICAL_SLICES S1 |
