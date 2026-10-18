# Desk backend (`desk`)

The Trading Desk Monitor backend. Design: [`docs/architecture/04_BACKEND.md`](../docs/architecture/04_BACKEND.md). Current state: **slices S1 and S2** (next: S3, snapshots from the VPS hub) of [`docs/architecture/VERTICAL_SLICES.md`](../docs/architecture/VERTICAL_SLICES.md). It values fixed-coupon RUB bonds from real hub snapshots with core 0.8.1, explains the day-over-day flash PnL by exact repricing, and publishes EOD runs that the Desk Monitor prototype shows.

```
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

Requirements: [uv](https://docs.astral.sh/uv/), the sibling checkouts `../rub-fixed-income-core` and `../market-data-server` (with the hub's release bundle in `dist/` and its snapshot-layer worktree).

```bash
tools/devdata/build_dev_snapshots.sh
```

This step is needed once. It restores the hub seed into `.devdata/pg` and exports real `eod_final` snapshots for 2026-10-09 … 2026-10-15 to `.devdata/snapshots/`, in about 3 minutes.

```bash
cd backend && uv sync --python 3.11 --extra dev
```

```bash
cd backend && .venv/bin/desk eod --snapshots ../.devdata/snapshots --date 2026-10-15 --book examples/demo_book.csv --db ../.devdata/tdm.sqlite --report-dir ../.devdata/reports
```

```bash
backend/.venv/bin/python -m tdm --db .devdata/tdm.sqlite serve --host 127.0.0.1 --port 8765
```

Then open:
- http://127.0.0.1:8765/desk-monitor.html for the Desk Monitor on the real run;
- `.devdata/reports/validation_<date>.html` for the comparison with MOEX analytics over the whole bond universe.

Run `desk eod` for an earlier date first if you want the previous-day curve moves and a date picker with history. When the snapshot folder holds an earlier date, `desk eod` also attaches the flash PnL against it (slice S2; `--no-pnl` turns it off).

`examples/demo_book.csv` is synthetic: real public instruments, invented quantities. Regenerate it with `desk demo-book`.

## Settings

`desk.settings` reads `DESK_*` environment variables, then `backend/.env`. Copy [`.env.example`](.env.example) to `.env`; it is git-ignored. `desk config` prints the effective values, with the token masked.

| Variable | Default | Meaning |
|---|---|---|
| `DESK_HUB_URL` | not set | The VPS hub, set per installation (the address is not kept in the repository); HTTPS only |
| `DESK_HUB_TOKEN_FILE` / `DESK_HUB_TOKEN` | not set | Bearer token of this consumer, scope `snapshots`. Keep it in a file outside the repository; the repository is public |
| `DESK_HUB_CA_BUNDLE`, `HTTPS_PROXY` | system | Corporate proxy and its TLS-inspection CA (inside the work network) |
| `DESK_SNAPSHOT_CACHE` | `.devdata/snapshots` | Verified bundles; `desk eod` and `desk demo-book` read here unless `--snapshots` is given |
| `DESK_SNAPSHOT_DROP` | not set | Folder an operator fills by hand when the hub is unreachable |
| `DESK_DB` | `.devdata/tdm.sqlite` | Run store (`--db`) |
| `DESK_REPORT_DIR` | not set | Validation reports (`--report-dir`) |

Pulling snapshots from the hub (`desk hub-check`, `desk sync`) is slice S3, which is not built yet. Until then the cache is filled by the dev export below. Plan: [VERTICAL_SLICES S3](../docs/architecture/VERTICAL_SLICES.md#s3-brief-live-hub-and-a-test-book).

## Tests

```bash
cd backend && .venv/bin/pytest -q
```

The tests run slices S1 and S2 on the hub's golden fixtures, vendored in `tests/fixtures/mdhub_v1/` (ten public bonds of 2026-10-15). The fixtures hold one date, so the S2 tests build the previous date by shifting the KBD parameters. Snapshots of the whole universe are local only.

## Layout

| Module | Role | Architecture |
|---|---|---|
| `marketdata/bundle.py` | Reads `<snapshot_id>.json.gz` bundles, verifies sha256 | ADR-002 |
| `marketdata/adapter.py` | Contract v1 → `BondTerms`, `BondQuote`, core `BusinessCalendar`; the only module that knows the wire format | ADR-003 |
| `curves/kbd.py` | KBD/ZCYC → core `DiscountCurve` (**temporary**, belongs to core package 0.6) | ADR-004 |
| `valuation/bonds.py` | Eligibility with reason codes, fixed schedule with put/call horizon, core pricing and risk at the trade date | 04 §1, 01 §3 |
| `pnl/flash.py` | Day-over-day flash PnL by exact repricing: carry, rolldown, rates, spread, other, with the identity check | VERTICAL_SLICES S2 |
| `orchestrator.py` | One EOD date: prepare snapshots and curve, value requests, attach the flash PnL | 04 §1 |
| `publish/tdm_run.py` | Writes the run through `tdm.writer.Run` (**slice shortcut** for the desk store) | ADR-006 later |
| `report/validation.py` | Universe-wide comparison with MOEX analytics | VERTICAL_SLICES S1 |
