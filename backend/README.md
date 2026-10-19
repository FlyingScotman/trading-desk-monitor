# Desk backend (`desk`)

The Trading Desk Monitor backend. Design: [`docs/architecture/04_BACKEND.md`](../docs/architecture/04_BACKEND.md). Current state: **slices S1–S3** (next: S4, the core portfolio path) of [`docs/architecture/VERTICAL_SLICES.md`](../docs/architecture/VERTICAL_SLICES.md). It pulls verified snapshots from the VPS hub into a local cache, values fixed-coupon RUB bonds from them with core 0.32.0 (pinned by commit from its GitHub repository), explains the day-over-day flash PnL by exact repricing, and publishes EOD runs that the Desk Monitor prototype shows.

```
VPS hub ──► desk.marketdata.hub + cache (desk sync) ──► snapshot cache (or a hand-filled drop folder)
                          │
hub snapshot files ──► desk.marketdata (bundle + adapter mdhub_v1)
                          │
                          ├─► desk.curves.kbd ──► core DiscountCurve
                          ▼
                       desk.valuation.bonds ──► core BondRiskEngine (Z-spread, KRD DV01, CS01, Hessian)
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
- `examples/test_book.csv`: the S3 test book. Three books (`OFZ`, `CORP`, `MIXED`), long and short, with the coverage cases that must show as not priced. Regenerate it with `desk demo-book --profile test --date D --out examples/test_book.csv`;
- `examples/demo_book.csv`: the S1 demo book (`desk demo-book`).

### Offline fallback

When the hub is unreachable (the proxy blocks it), an operator copies `<snapshot_id>.json.gz` bundles into `DESK_SNAPSHOT_DROP` and runs `desk import-drop`. Each file is verified like a download; a bad file is rejected with its reason and not copied.

Without any hub, the dev export restores the hub seed into `.devdata/pg` and exports real `eod_final` snapshots for 2026-10-09 … 2026-10-15 to `.devdata/snapshots/` (about 3 minutes). It needs the sibling checkout `../market-data-server` with the hub's release bundle in `dist/` and its snapshot-layer worktree:

```bash
tools/devdata/build_dev_snapshots.sh
```

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

The tests run slices S1–S3 on the hub's golden fixtures, vendored in `tests/fixtures/mdhub_v1/` (ten public bonds of 2026-10-15). The fixtures hold one date, so the S2 tests build the previous date by shifting the KBD parameters. The S3 tests serve the same fixtures from an in-process stub hub on `127.0.0.1` (paging, 401/403/404, retries, corrupt bodies, the drop folder, the token never printed). Snapshots of the whole universe are local only.

`pytest -m live` checks the real hub in `DESK_HUB_URL` (hub-check and a sync of its latest date); the default run never touches the network.

## Layout

| Module | Role | Architecture |
|---|---|---|
| `marketdata/hub.py` | HTTP client of the hub's `/v1/snapshots` API; the only module that knows the hub's paths | ADR-001 §4 |
| `marketdata/cache.py` | `sync` and `import_drop`: verified, atomic, append-only writes into the cache | ADR-001 §4 |
| `marketdata/bundle.py` | Reads `<snapshot_id>.json.gz` bundles, verifies sha256 | ADR-002 |
| `marketdata/adapter.py` | Contract v1 → `BondTerms`, `BondQuote`, core `BusinessCalendar`; the only module that knows the wire format | ADR-003 |
| `curves/kbd.py` | KBD/ZCYC → core `DiscountCurve` (**temporary**: core 0.11 has the evaluator; replaced in S4) | ADR-004 |
| `valuation/bonds.py` | Eligibility with reason codes, fixed schedule with put/call horizon, core pricing and risk at the trade date | 04 §1, 01 §3 |
| `pnl/flash.py` | Day-over-day flash PnL by exact repricing: carry, rolldown, rates, spread, other, with the identity check | VERTICAL_SLICES S2 |
| `orchestrator.py` | One EOD date: prepare snapshots and curve, value requests, attach the flash PnL | 04 §1 |
| `publish/tdm_run.py` | Writes the run through `tdm.writer.Run` (**slice shortcut** for the desk store) | ADR-006 later |
| `report/validation.py` | Universe-wide comparison with MOEX analytics | VERTICAL_SLICES S1 |
