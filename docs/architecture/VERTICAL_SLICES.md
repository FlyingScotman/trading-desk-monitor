# Vertical slices

As of 18 October 2026. Status: working plan, owned by the backend. **Next slice: S3, brief below.**

## Why

Until now every component grew on its own: the hub collects and now publishes snapshots, core prices and risks, the prototypes show a mock desk, and the backend exists only as documents. None of the pieces has been checked against the others on real data.

From here, work goes in **vertical slices**. A slice is a thin path through every layer that the owner can open and check:

- it runs on **real hub data** (public market and static data);
- it uses **real core calculations**;
- it shows the result in a screen.

Each slice keeps whatever it built that matches the architecture. Where it takes a shortcut, the shortcut is named, and a later slice replaces it. ROADMAP milestones M1–M7 stay the destination; the slices are the order in which we reach them.

## Shortcuts the slices take on purpose

| Architecture target | Slice 1 shortcut | Replaced in |
|---|---|---|
| Backend reads snapshots from the hub API (`/v1/snapshots`) | Reads bundle files that the hub's own builder produced from a copy of the seed database (`tools/devdata/build_dev_snapshots.sh`) | S3. The dev export stays as the offline fallback |
| Desk Postgres store with run lineage (ADR-006) | The `tdm` SQLite store through its writer SDK (`tdm.writer.Run`); lineage goes into the run log and `lib_version` | S5 |
| Desk API v1 + React screens (05) | `tdm serve` and the existing Desk Monitor prototype through `window.DESK` | S5 (API), S6 (first React screen) |
| KBD/ZCYC evaluator in core (core package 0.6) | Backend module `desk.curves.kbd`, which builds a core `DiscountCurve` from the published ZCYC parameters | When core delivers 0.6 |
| Real positions (Q1 deferred) | A synthetic book over real instruments (`backend/examples/demo_book.csv`; S3 adds `test_book.csv` with several books and coverage cases) plus a "universe" run: one bond of every priceable instrument | When Q1 is answered |

## Slices

| Slice | What the owner sees | Checks which pieces | Status |
|---|---|---|---|
| **S1 EOD fixed bonds on KBD** | Desk Monitor with a real 15.10.2026 run: PV, yield, Z-spread, DV01 by bucket, CS01, gamma for fixed-coupon OFZ and corporates of the demo book. A **validation report** per bond: our yield, duration, G-spread and accrued vs MOEX analytics from the same snapshot; coverage list with a reason for every bond not priced | Hub snapshot contract on the full universe; adapter; core 0.8.1 fixed-cashflow engine; KBD curve; writer SDK; prototype data path | **Done 17.10.2026** (see below) |
| **S2 Day-over-day flash PnL** | Second run on the next date with PnL by factor: carry, rates, spread, residual, using two snapshots and exact repricing | Snapshot history; core `reprice`; run-to-run linking | **Done 17.10.2026** (see below) |
| **S3 Live hub and a test book** | Desk Monitor on runs of the last five business dates, valued from snapshots **pulled from the VPS hub** into the backend's cache. A synthetic test book with several books, shorts and coverage cases. Snapshot age on every run | Hub snapshot endpoints and token scopes; backend HTTP client, cache and verification; reproducibility by snapshot ID; the run never depends on the hub being up | **Next.** Live acceptance waits for the hub's snapshot layer (H1–H5 below) |
| **S4 Floaters and the RUONIA curve** | OFZ-PK and key-rate floaters priced with fixings and projection; a RUONIA OIS curve from the hub's quote sets in the curves panel | Hub fixings and quote sets; core `FloatingLeg`, OIS bootstrap; curve-spec mapping `dataset` → spec | |
| **S5 Desk backend proper** | Same screens, now served by the desk API from the desk Postgres store, with run lineage | ADR-006, 04, 05 §2 | |
| **S6 Interactive scenario** | Scenario slider with instant Taylor (≈) and an "exact" button that reprices on the backend; residual shown | 03 T1/T2, parity fixtures; first React + TypeScript screen | |
| **S7 Intraday** | Intraday run every N minutes on intraday cuts; badge with age | Hub intraday cuts; scheduler | |
| **S8 Hybrid book** | IRS, FX and other products imported from contract 1.3 files next to computed bonds, with coverage states | `private_inputs`, hybrid merge | |

Each slice ends with:
- a short note in this file: what works, what was found, what moved to the backlog of which repo;
- a test that runs the slice on the hub's golden fixtures in CI. Real-data runs stay local, because the snapshots are not committed.

## S3 brief: live hub and a test book

This section is the work order for the backend session. It is self-contained; the architecture behind it is [ADR-001](adr/ADR-001-hosting-topology.md) §4 (snapshot pull, cache, drop folder), [ADR-002](adr/ADR-002-materialized-market-snapshots.md) and [04](04_BACKEND.md) §1 (`marketdata`).

**Goal.** The owner runs the backend on a development machine with only a hub token and the core checkout. They pull real published snapshots from the VPS hub, run EOD and the flash PnL of a synthetic test book over several dates, and check the numbers in the Desk Monitor. No hub checkout and no seed restore are needed.

**State on 18 October 2026.**
- The hub is live on the VPS: `/health/ready` answers `ok`, hub 0.1.0, 23 migrations applied.
- **No consumer token exists yet.** Without a token, every existing `/v1` route answers 401 (for example `/v1/bonds`, `/v1/zcyc`). That is the expected fail-closed behaviour.
- `/v1/snapshots` answers **404** even without a token, while the existing routes answer 401. So the route is missing, not refused. The snapshot layer is not merged into hub `main` and not deployed: migrations 0500/0510, `ops.published_snapshot`, jobs `snapshot_static` and `snapshot_market`, routes `/v1/snapshots*`. It lives only in hub branch `claude/silly-tharp-ed59c7`. A token alone does not fix this; H1 does.
- Backend settings are in place: `desk.settings`, `backend/.env.example` and `desk config`. The hub address has no default: it is set per installation in `DESK_HUB_URL` (environment or `backend/.env`) and is never written into the repository, including tests and fixtures. Defaults: cache `.devdata/snapshots`, store `.devdata/tdm.sqlite`.
- `SnapshotDir` reads and verifies bundle files from a folder; S1 and S2 run from that folder.

### Prerequisites on the hub (the owner's hub session)

| # | What | Check |
|---|---|---|
| H1 | Merge the snapshot layer into hub `main`, renumbering its migrations after the current ones (F7). Release it and deploy it to the VPS | `/health/ready` shows the new migration count, with none pending |
| H2 | Give the API's database role read access to `ops.published_snapshot` (service roles) | The list call below answers 200, not 500 |
| H3 | Schedule `snapshot_static` daily and `snapshot_market` for `eod_final` (and `eod_preliminary`). Cut times come from `MDHUB_SNAPSHOT_*` (DEC04 is pending) | A new `eod_final` appears after each business day |
| H4 | Publish `eod_final` once for the last ten business dates, to give the flash PnL and the date picker a history. These carry today's `known_at` (F6); this is accepted for testing | The list call shows ten dates |
| H5 | Create a named token `desk-dev` with scope `snapshots`: on the console's Tokens page, or on the VPS with `mdhub tokens create desk-dev --scope snapshots > hub_token` (the token is shown once). Store it outside the repository, for example `~/.config/desk/hub_token`. A second token `dev-read` (scope `read`) is useful for checking `/v1/coverage` and the raw series. H5 can be done now, before H1 | `/v1/coverage` answers 200 with the `read` token; `desk hub-check` passes after H1 |

List call: `GET <DESK_HUB_URL>/v1/snapshots?kind=market&cut_type=eod_final&latest=true`, header `Authorization: Bearer <token>`.

The backend work B1–B6 does not wait for H1–H5: it is built and tested against a local stub hub. Only the live acceptance waits.

### Backend work

**B1. Hub client:** `desk/marketdata/hub.py`, the only module that knows the hub's HTTP paths. The wire format stays in `adapter.py`.
- `HubClient(settings)` offers:
  - `health()` → `/health/ready`;
  - `list_snapshots(kind, cut_type, business_date, latest)` → `/v1/snapshots`, following `meta.next_offset`;
  - `manifest(id)`;
  - `bundle(id) -> bytes`.
- Transport is `httpx` (add it to the dependencies; it is a pure-Python wheel and fits the Windows wheelhouse). It honours `HTTPS_PROXY`/`NO_PROXY`, `DESK_HUB_CA_BUNDLE` and `DESK_HUB_TIMEOUT_S`.
- The bearer token is sent only in the header. It is never logged, printed, put in a URL or written to the run.
- Errors and what the user is told:

  | Response | Message |
  |---|---|
  | No `DESK_HUB_URL` set (no request made) | Hub address not configured; set `DESK_HUB_URL` |
  | 401 | Token rejected |
  | 403 | Token lacks scope `snapshots` |
  | 404 on `/v1/snapshots` | The hub has no snapshot layer (give its version from `/health/ready`) |
  | 5xx or a connection error | Retried three times with backoff, then fail with the last error |

**B2. Snapshot cache:** `desk/marketdata/cache.py`, writing into `DESK_SNAPSHOT_CACHE`. `SnapshotDir` stays the only reader.
- `sync(client, cache, dates, cut_type="eod_final")`. For each date:
  1. take the newest non-superseded market snapshot (`latest=true`);
  2. download its bundle unless it is already cached;
  3. verify `sha256(gunzip(body)) == id`, and that `X-Snapshot-Sha256` matches;
  4. write `<id>.json.gz` atomically (`.part`, then rename);
  5. fetch the static snapshot named by `static_snapshot_id` the same way.
- A corrected snapshot is written beside the old one; `SnapshotDir.market()` already picks the one that is not superseded.
- The cache is append-only: files are never rewritten or deleted by the backend.
- `import_drop(drop, cache)` copies bundles from `DESK_SNAPSHOT_DROP` with the same verification. A bad file is rejected with a reason and not copied. This is the fallback when the proxy blocks the VPS (ADR-001 §4).

**B3. Commands.**
- `desk hub-check`. It reports:
  - the hub version and migrations;
  - whether the token is accepted;
  - the latest `eod_final` date and its age.

  It exits non-zero with the reason.
- `desk sync --date D | --from D1 --to D2 | --last N [--cut eod_final]`. With `--last N`, the dates come from the hub's list.
- `desk eod --sync` pulls the date and the business date before it first. Without `--sync`, `desk eod` reads only the cache, so a run never depends on the hub being up.
- Every run prints and records the market snapshot ID, its `published_at` and its age.

**B4. Lineage**, until the S5 store. Record in the `tdm` run log and `lib_version`, as S1 does:
- the hub host;
- the market and static snapshot IDs, `cut_type` and `published_at`;
- the backend git SHA and the core version.

If the prototype has a place for it in `window.DESK` meta, show the snapshot age in the Desk Monitor; otherwise show it in the run log.

**B5. Test book:** `backend/examples/test_book.csv`. Instruments are public; quantities are invented.
- Three books (`OFZ`, `CORP`, `MIXED`), 40–60 positions, long and short.
- It contains:
  - OFZ-PD across the curve;
  - corporates over a range of spreads;
  - an amortizer;
  - a bond with a put within six months;
  - a bond with a coupon paid inside the test window, so the received cash shows in the PnL.
- Coverage cases that must show as not priced, each with a reason:
  - OFZ-PK;
  - a key-rate floater;
  - OFZ-IN;
  - a perpetual;
  - a bond with no quote on one of the dates.
- It is generated by `desk demo-book --profile test` from the latest cached date, then committed, so every date uses the same book.
- **Real desk positions never enter the repository.** The repository is public ([06](06_DEPLOYMENT_AND_OPERATIONS.md) rules).

**B6. Tests in CI, without network.**
- Client and cache tests run against an in-process stub hub (`http.server` in a thread on `http://127.0.0.1`), serving the vendored fixtures `tests/fixtures/mdhub_v1/`. They cover:
  - list, `latest`, manifest, bundle with `ETag` and 304;
  - 401, 403, and 404 for a hub without the snapshot layer;
  - a 500 followed by success (retry);
  - a truncated body or a wrong hash: rejected, and nothing written;
  - the atomic write;
  - the drop import;
  - the token appearing in no output or log line.
- `pytest -m live` runs only when `DESK_HUB_URL` and a token are configured: `hub-check` and a sync of the latest date against the hub in `DESK_HUB_URL`.

**B7. Documentation.**
- `backend/README.md` "Run it locally" leads with the hub path: `backend/.env`, `desk hub-check`, `desk sync --last 5`, `desk eod`.
- The dev export (`tools/devdata/`) moves to "Offline fallback".
- Add an S3 result note to this file.

### The owner's check: frontend and backend on the test book

1. Copy `backend/.env.example` to `backend/.env`, set `DESK_HUB_URL` to the hub's address, put the token file in place, then run `desk config` and `desk hub-check`.
2. Run `desk sync --last 5`, then `desk eod --book examples/test_book.csv --report-dir ../.devdata/reports` for each date, oldest first.
3. Run `tdm serve` and open the Desk Monitor. Check:
   - the date picker over the five dates;
   - positions by book; DV01 by bucket; CS01;
   - the PnL waterfall (carry, rolldown, rates, spread);
   - the coverage list with reasons;
   - the snapshot age.
4. In the validation report of the latest date, yields agree with MOEX as in S1: OFZ-PD median |Δy| at most 0.3 bp, corporates at most 1 bp.

### Acceptance

- From a clean checkout with the hub address, a token and the core checkout, `sync` and `eod` work over five dates.
- A rerun on the same snapshot IDs gives identical position results.
- With the hub unreachable (wrong URL or no network), `desk eod` on cached dates still works, and `desk sync` fails with a clear message.
- A wrong token gives the 401 message. The token appears in no output, log or run.
- Every position of the test book is priced or listed with a reason. The flash PnL identity holds to 0.01 RUB.
- Where a VPS-published snapshot and a dev-export snapshot exist for the same date, compare their instrument counts and quotes. Record the differences as findings (F6).

**Out of scope:**
- the desk Postgres store and API (S5);
- Windows services and the corporate proxy (Q4). The settings already carry `DESK_HUB_CA_BUNDLE` and `HTTPS_PROXY`;
- floaters (S4);
- intraday cuts (S7).

## S1 detail

**Flow.**

1. Market snapshot of `D`, plus the static snapshot it names.
2. `desk.marketdata.bundle` reads the files and verifies the sha256.
3. `desk.marketdata.adapter` builds instruments: the known coupon schedule plus redemptions, the next put offer as horizon, and the selected quote with its basis.
4. `desk.curves.kbd` turns the ZCYC parameters into a `DiscountCurve`.
5. `desk.valuation.bonds` runs core `BondRiskEngine` (`continuous_zero` basis):
   - Z-spread calibrated to the dirty price;
   - bucket DV01 / KRD, CS01 or OFZ spread risk;
   - Hessian;
   - yield and duration through core `compatibility`.
6. `desk.publish.tdm_run` writes one `tdm` EOD run.
7. `desk.report.validation` writes the validation report.

**Pricing scope** (everything else is listed with a reason, never dropped):
- RUB, fixed coupon;
- every future coupon period `known`, with an amount;
- a redemption schedule summing to the current face;
- not perpetual, not structured, not face-indexed.

**Price basis** (provisional until a DEC decides it): MOEX legal close, then close, then weighted average. The basis used is stored on every position.

**Acceptance.**
- Every OFZ-PD and fixed corporate bond in scope is priced or rejected with a reason.
- For bullets priced to maturity, our yield at the MOEX price basis agrees with the MOEX `yield_close_pct` to within 1 bp. Larger differences are listed as findings for the hub or core backlog.
- Desk Monitor opens on the run.

## S1 result (17 October 2026)

How to run it: [`backend/README.md`](../../backend/README.md).

**What works.**
- Real `eod_final` snapshots for 09–15.10.2026 are rebuilt from the hub seed with the hub's own builder:
  - 4214 instruments, about 3440 bond quotes per day;
  - the KBD reproduces MOEX standard yields to 0.005 bp.
- One EOD date takes about 7 s:
  - 1034 bonds priced over the whole universe, with first-order risk, in about 3 s;
  - the 28-position demo book with the full Hessian;
  - the validation report.
- The Desk Monitor opens on the runs of 14 and 15 October: positions, bonds, DV01 by bucket, CS01, curve moves.
- Every bond not priced has a reason in the run log and the report.

**Comparison with MOEX, 15.10.2026.** Δy is the effective yield at the weighted-average price, measured from the trade date.

| Class | Priced | ok (≤ 1 bp) | warn (≤ 10 bp) | mismatch | Median abs Δy |
|---|---|---|---|---|---|
| OFZ-PD | 30 of 31 | 26 | 4 | 0 | 0.24 bp |
| Corporate | 949 of 2368 | 441 | 269 | 89 | 0.69 bp |
| Subfederal | 46 of 63 | 17 | 20 | 3 | 1.57 bp |

Most mismatches are bonds 1–3 months from their horizon. There a price difference of a few kopecks is many basis points of yield, while durations agree to 1–2 days.

**Findings and where they go.**

| # | Finding | Goes to |
|---|---|---|
| F1 | MOEX measures effective yield and duration from the **trade date**, not the T+1 settlement date. With T+1 the bias was +0.9 bp (OFZ) and +4 bp (corporates); with the trade date it is −0.2 / −0.1 bp | Done in the report. A DEC entry must fix which date the desk shows yields from |
| F2 | The yield horizon is the next offer **whose exercise window is still open**, including issuer calls; MOEX skips puts whose window has closed | Done in `valuation/bonds.py`. Core's future `Instrument` (package 0.1) needs `window_end` and the call/put type |
| F3 | 314 instruments have a quote without accrued interest, and 130 have no price (no trades). Every instrument carries `day_count_not_published`, so neither the backend nor core can compute accrued itself | Hub: publish MOEX accrued for every instrument (securities board data), not only for traded ones. Core: accrued from the schedule needs the day count |
| F4 | Some `known` coupon amounts contradict MOEX accrued. Example: RU000A101M04, next coupon 30.21 RUB against accrued 36.45 RUB | Hub: bond static verification (hub STATUS, remaining step 3) |
| F5 | For bonds close to their horizon, comparing yields is not meaningful | Backend: compare in price space next |
| F6 | The dev export builds every historical cut with today's `known_at` | Accepted for development. Real runs read published snapshots (S3) |
| F7 | The hub's snapshot layer sits in a worktree based on an older main (migrations 0423/0424 missing there) | Hub: merge the snapshot layer (S3, H1) |
| F8 | Distressed names dominate "most traded" corporates (yields of 200–400 %) | Demo book filters them out; the real book will contain whatever the desk holds |

The S1 comparison table above was measured with the T+1 valuation date. S2 moved the valuation to the trade date (finding F9 below). On 15.10 the corporate row is now 452 ok, 272 warn, 75 mismatch, with a median of 0.65 bp. The subfederal row is 19 ok, 20 warn, 1 mismatch, median 1.18 bp. The OFZ-PD row is unchanged.

## S2 result (17 October 2026)

**How to run it.** `desk eod` now attaches the flash PnL whenever the snapshot folder holds an earlier `eod_final` date; `--no-pnl` turns it off. Run the dates in order to get the history:

```bash
cd backend && for d in 2026-10-12 2026-10-13 2026-10-14 2026-10-15; do .venv/bin/desk eod --snapshots ../.devdata/snapshots --date $d --book examples/demo_book.csv --db ../.devdata/tdm.sqlite; done
```

Each date takes about 2 s; the previous date is revalued on its own snapshot. The book is the same on both dates, so there are no trades of the day.

**Method** (`backend/src/desk/pnl/flash.py`, exact repricing with core 0.8.1, no Taylor). The D-1 position (its cashflows, quantity and calibrated Z-spread `z0`) is repriced step by step through core `BondRiskEngine` with `spread_bp` as input. Each step changes one thing:

| Step | Curve | Date | Spread | Component (difference to the step above) |
|---|---|---|---|---|
| V0 | KBD of D-1 | D-1 | `z0` | equals the D-1 dirty value |
| V_carry | KBD of D-1 | **D** | `z0` | `carry`: time on unchanged forwards and spread; coupons and redemptions paid in (D-1, D] are added here |
| V_roll | ZCYC parameters of D-1, dated D | D | `z0` | `rolldown`: "same yield per tenor" vs "forwards realized" |
| V_rates | **KBD of D** | D | `z0` | `rates` |
| V_spread | KBD of D | D | **`z1`** (calibrated on D) | `spread` |
| V1 | | | | `other`: the D dirty value minus V_spread. It is not zero when the D-1 cashflows differ from the D schedule, for example after an offer passed |

- `rates` and `spread` are full repricings, so second-order effects are inside them. `convexity` is 0 on purpose.
- Check per position: `carry + rolldown + rates + spread + other = q·(dirty_D − dirty_D-1) + received`, where both dirty prices come from the published valuations. If the error is more than 0.01 RUB, the position gets no PnL and a WARN `identity_failed`.
- A position not priced on either date gets no `pnl` and a WARN with the reason, for example `not_priced_prev: 2026-10-13: no_quote`. A stale quote (the same trade date on both days) or a changed quantity is treated the same way.
- Each position's `extra.pnl` keeps the diagnostics: `z0`, `z1`, cashflows received, total, identity error, and `rates_linear` (Σ D-1 bucket DV01 × zero-rate move per pillar).

**What works.**
- The Desk Monitor PnL tab shows real flash PnL for 12–15.10 instead of "positions without PnL". It has the waterfall by component, books, and the 22-day history with explained PnL. Official PnL stays "expected T+1".
- The identity holds on every position and every date: the largest error is 3·10⁻⁴ RUB on positions of up to 2 bn RUB.
- On 15.10 the book made +34.78 m RUB: carry +4.80, rolldown +1.68, rates +24.36, spread +3.94, other 0.

Demo book, m RUB:

| Date | Book | Positions | Carry | Rolldown | Rates | Spread | Other | Total | Received | Σ\|rates − linear\| / Σ\|rates\| |
|---|---|---|---|---|---|---|---|---|---|---|
| 12.10 (3 days) | OFZ | 12 | 9.55 | 4.38 | 77.25 | −23.12 | 0 | 68.05 | 0 | 1.9 % |
| | CORP | 15 | 4.15 | 1.28 | 1.72 | 4.15 | 0 | 11.31 | 6.57 | 16 % |
| 13.10 | OFZ | 12 | 3.13 | 1.50 | 25.47 | −9.83 | 0 | 20.27 | 0 | 2.3 % |
| | CORP | 15 | 1.34 | 0.45 | 1.07 | −2.03 | 0 | 0.83 | 0 | 20 % |
| 14.10 | OFZ | 12 | 3.25 | 1.38 | 26.89 | 3.76 | 0 | 35.28 | 66.76 | 0.9 % |
| | CORP | 15 | 1.38 | 0.41 | 1.43 | −5.99 | 0 | −2.77 | 3.60 | 16 % |
| 15.10 | OFZ | 12 | 3.32 | 1.28 | 19.78 | 8.07 | 0 | 32.46 | 0 | 1.3 % |
| | CORP | 16 | 1.47 | 0.40 | 4.58 | −4.13 | 0 | 2.32 | 3.12 | 8 % |

CRP-05 has no PnL on 14.10: it had no quote on 13.10.

**Findings and where they go.**

| # | Finding | Goes to |
|---|---|---|
| F9 | MOEX accrued is measured to the **trade date**. Over 09–15.10, 6661 of 7675 quotes in a coupon period match the accrued to the trade date and none match T+1; the other 1014 match neither (different day counts). The bond trades cum-coupon through the record date. S1 discounted from T+1 and dropped cashflows on or before T+1. On a record date it therefore lost the coupon paid the next day while the price still contained its accrued. Example: ODK RU000A10FG76 on 15.10, Z 266 → 194 bp, and the PnL counted the 4.07 m RUB coupon twice. The valuation date is now the trade date for every bond; T+1 is kept as `settlement` for information. The S1 comparison improved (see above) | Done in `valuation/bonds.py`. DEC entry: valuation and yield date = trade date (closes F1) |
| F10 | OFZ spreads to the KBD move by several bp a day (SU26228: −51 → −43 bp on 15.10). MOEX's published Z-spreads move in the same direction (SU26254: 39 → 36 bp). This puts −23 m to +8 m RUB a day of the OFZ book into `spread`. The KBD is a smooth fit, so part of the daily OFZ move is the fit's residual, not the curve | Desk: decide whether OFZ show `spread` vs KBD or a curve fitted to the desk's own OFZ. Hub: nothing |
| F11 | The KBD short end is volatile: on 15.10 1M moved +36 bp and 3M +27 bp, while 2–5Y moved −8 to −11 bp. For short corporates, pillar DV01 × pillar move misses 8–20 % of the exact `rates`, because the curve's move between pillars is not linear in the pillar moves. For OFZ it misses 1–2 % | Expected. S5 (Taylor vs exact) should show this residual. The "Rates explain" matrix of the Desk Monitor will not match `rates` for short bonds |
| F12 | The legal close and the close can differ by 0.5 % on a liquid bond (Polyplast 15.10: 98.30 vs 98.83). The day's PnL then depends on the price basis: with 250 000 bonds that is 1.3 m RUB | DEC on the price basis (open since S1) |
| F13 | `other` was 0 on every position: no schedule changed between two dates of the demo book. A position whose offer passes, or with a newly fixed coupon, will show it here | Check again on the real book |
| F14 | Rolldown on the KBD is 30–40 % of carry on these dates | Information |

