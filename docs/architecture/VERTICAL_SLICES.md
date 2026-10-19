# Vertical slices

As of 18 October 2026. Status: working plan, owned by the backend. **S3 and S4 are built (19 October; the S3 live acceptance waits for the hub's snapshot layer). Next: S5.** Work for the hub and core: [briefs/HUB.md](briefs/HUB.md), [briefs/CORE.md](briefs/CORE.md).

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
| KBD/ZCYC evaluator in core (delivered in core 0.11) | Backend module `desk.curves.kbd`, which builds a core `DiscountCurve` from the published ZCYC parameters | **Replaced in S4** (core `rub_ofz_kbd_v1`) |
| Core's portfolio path: `value_portfolio` over `Instrument`/`MarketSnapshot`/`Position` (core 0.10–0.11) | The adapter builds `BondCashflow` totals and calls `BondRiskEngine` per position | **Replaced in S4**. The fallback rungs below core still reprice with `BondRiskEngine` |
| Horizon PnL explain in core: `explain_horizon_pnl`, `horizon_portfolio` (core 0.14) | `desk.pnl.flash`, the backend's own stepwise repricing | **Compared in S4: `desk.pnl.flash` stays** (S4 result); `pnl/compare.py` is the regression check |
| Real positions (Q1 deferred) | A synthetic book over real instruments (`backend/examples/demo_book.csv`; S3 adds `test_book.csv` with several books and coverage cases) plus a "universe" run: one bond of every priceable instrument | When Q1 is answered |

## Slices

| Slice | What the owner sees | Checks which pieces | Status |
|---|---|---|---|
| **S1 EOD fixed bonds on KBD** | Desk Monitor with a real 15.10.2026 run: PV, yield, Z-spread, DV01 by bucket, CS01, gamma for fixed-coupon OFZ and corporates of the demo book. A **validation report** per bond: our yield, duration, G-spread and accrued vs MOEX analytics from the same snapshot; coverage list with a reason for every bond not priced | Hub snapshot contract on the full universe; adapter; core 0.8.1 fixed-cashflow engine; KBD curve; writer SDK; prototype data path | **Done 17.10.2026** (see below) |
| **S2 Day-over-day flash PnL** | Second run on the next date with PnL by factor: carry, rates, spread, residual, using two snapshots and exact repricing | Snapshot history; core `reprice`; run-to-run linking | **Done 17.10.2026** (see below) |
| **S3 Live hub and a test book** | Desk Monitor on runs of the last five business dates, valued from snapshots **pulled from the VPS hub** into the backend's cache. A synthetic test book with several books, shorts and coverage cases. Snapshot age on every run | Hub snapshot endpoints and token scopes; backend HTTP client, cache and verification; reproducibility by snapshot ID; the run never depends on the hub being up | **Built** 19 October ([result](#s3-result-19-october-2026)). Live acceptance waits for the hub's snapshot layer (H1–H4 below) |
| **S4 Core portfolio path and the whole bond universe** | The same screens, valued through core 0.32 `value_portfolio`. Also priced now: floaters (OFZ-PK, key-rate, RUONIA), perpetuals to an explicit call and, once the hub carries CPI, OFZ-IN. A RUONIA OIS curve (`rub_ruonia_ois_moex_v2`) in the curves panel. Coverage grows from about 1034 bonds to every bond with a usable schedule | Adapter to core domain types; curve-spec mapping `dataset` → spec; core KBD; core horizon explain against `desk.pnl.flash` | **Built** 19 October ([result](#s4-result-19-october-2026)) |
| **S5 Desk backend proper** | Same screens, now served by the desk API from the desk Postgres store, with run lineage | ADR-006, 04, 05 §2 | |
| **S6 Interactive scenario** | Scenario slider with instant Taylor (≈) and an "exact" button that reprices on the backend; residual shown | 03 T1/T2, parity fixtures; first React + TypeScript screen | |
| **S7 Intraday** | Intraday run every N minutes on intraday cuts; badge with age | Hub intraday cuts; scheduler | |
| **S8 Derivatives and the hybrid book** | IRS, FX forwards and swaps, XCCY, futures and REPO from contract 1.3 term files, valued by core (result v3) next to bonds; CDS and whatever still lacks inputs imported, with coverage states | `private_inputs`; snapshot inputs from [briefs/HUB.md](briefs/HUB.md) (futures prices, FX value dates, RUONIA index); hybrid merge | |

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
- Transport: the standard library (`urllib`; the brief first named `httpx`, see the S3 result). It honours `HTTPS_PROXY`/`NO_PROXY`, `DESK_HUB_CA_BUNDLE` and `DESK_HUB_TIMEOUT_S`.
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

## S4 brief: core portfolio path and the whole bond universe

**Goal.** Bonds are valued through core 0.32's `value_portfolio`, the entry point core built for the desk (core `docs/CONSUMERS.md`, "Desk backend"). The backend's own bond plumbing goes away, and coverage grows to every bond with a usable schedule. S4 needs no hub change beyond what the snapshot already carries, except CPI for OFZ-IN. It can run in parallel with S3, because both sides read the same `SnapshotDir`.

**Work.**
1. **Adapter to core domain types** (`marketdata/adapter.py`): snapshot → core `Instrument`, `MarketSnapshot` and `Position`, in this one module (ADR-003).
   - Coupon legs per core `CONSUMERS.md`:
     - OFZ: `FixedCouponLeg`;
     - corporates: `RateAccrualCouponLeg` with `AccrualBasis.RATE_ACT_365` and each coupon's `valueprc / 100`;
     - ACT/360 and 30/360 issues: the matching basis.
   - Offers mapped to `Offer`. The open-window rule of F2 stays in the backend until core C1 lands.
   - Floaters through the snapshot's `floater_formulas`, used only with status `curated` (or `inferred`, if the owner accepts it). Otherwise the bond is rejected, never priced on guessed coupons (ADR-004).
2. **Curves through core** (ADR-004):
   - KBD by core's evaluator under `rub_ofz_kbd_v1`. Delete `desk.curves.kbd` once the curves agree to 0.01 bp on the dev snapshots.
   - RUONIA OIS by `rub_ruonia_ois_moex_v2` from the hub's quote sets.
   - The key-rate projection curve `rub_key_rate_projection_v1`.
   - The mapping hub `dataset` → core spec ID lives in the backend as a versioned table (README interface catalogue).
3. **Valuation** (`valuation/bonds.py`): one `value_portfolio` call per run.
   - `ValuationPolicy` holds the spread convention and the cap/floor guard. The trade-date valuation (F9) is not a policy field in core 0.32: the backend builds instruments with `settlement_lag_days=0` (S4 result).
   - Perpetuals and callables carry explicit `exercise_assumptions` per DEC10; without one they are rejected.
   - Store the hash-verified result (`rub-portfolio-risk-result.v2`) with the run.
4. **PnL.** Compute the flash PnL both ways: `desk.pnl.flash` and core `explain_horizon_pnl` / `horizon_portfolio`. Report the difference per component. Keep one implementation once they agree, or record why they differ.
5. **Tests.** The S1/S2 tests keep their tolerances on the vendored fixtures. Add new fixture cases for the floater, the OFZ-IN and the perpetual already in `tests/fixtures/mdhub_v1/`.

**Acceptance.**
- On the dev snapshots of 09–15.10, fixed-coupon results match S1/S2:
  - PV within 1 RUB per position;
  - Z-spread within 0.1 bp;
  - flash PnL total within 0.01 %.

  Otherwise each difference is explained as a finding.
- OFZ-PK and corporate floaters are priced, with their coupons checked against MOEX accrued (core 0.26 goldens).
- The validation report covers the new classes, and the "not priced" list shrinks to bonds without a usable schedule or formula. Each one has a reason.
- `desk.curves.kbd` is deleted.

**Out of scope:**
- derivatives (S8);
- OFZ-IN, until the hub carries a CPI series ([briefs/HUB.md](briefs/HUB.md) H10).

## Core 0.32.0 recheck (18 October 2026)

The backend moved from a local core 0.8.1 checkout to core 0.32.0, pinned by commit from its GitHub repository (`backend/pyproject.toml`). Without code changes beyond one deprecated call (`solve_continuous_z_spread` → `solve_z_spread` with a continuous `ZSpreadConvention`):
- the tests pass, also with `DeprecationWarning` as an error;
- the validation report of 15.10 over 4033 instruments is byte-identical to the one on 0.8.1;
- the flash PnL of 14→15.10 is unchanged: +34.776 m RUB, 28/28 positions.

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
| F2 | The yield horizon is the next offer **whose exercise window is still open**, including issuer calls; MOEX skips puts whose window has closed | Done in `valuation/bonds.py`. Core 0.32 `Offer` has a date, a type and `confirmed`, but no exercise window, and its horizon uses confirmed puts only: [briefs/CORE.md](briefs/CORE.md) C1 |
| F3 | 314 instruments have a quote without accrued interest, and 130 have no price (no trades). Every instrument carries `day_count_not_published`, so neither the backend nor core can compute accrued itself | Hub: publish MOEX accrued for every instrument, [briefs/HUB.md](briefs/HUB.md) H7. The coupon rate (`valueprc`) is already in the static snapshot, so core 0.17 can compute accrued from the schedule with the rate basis (S4) |
| F4 | Some `known` coupon amounts contradict MOEX accrued. Example: RU000A101M04, next coupon 30.21 RUB against accrued 36.45 RUB | Hub: bond static verification, [briefs/HUB.md](briefs/HUB.md) H8 |
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

## S3 result (19 October 2026)

**Built** (backend B1–B7):
- `desk/marketdata/hub.py`: the hub client. It uses the standard library rather than `httpx`: `urllib` honours `HTTPS_PROXY`/`NO_PROXY`, and on Windows Python trusts the system certificate store, where a TLS-inspecting proxy's CA usually sits; `httpx` would need `DESK_HUB_CA_BUNDLE` for that. No new dependency. The error table of B1 is implemented as written; the 404 message names the hub version from `/health/ready`.
- `desk/marketdata/cache.py`: `sync`, `hub_dates`, `import_drop`. Each bundle is checked (`gzip`, `sha256 == id`, `X-Snapshot-Sha256`, schema version) before an atomic write; the cache is append-only. Beside a downloaded bundle, `<id>.hub.json` keeps the hub host, `published_at` and the fetch time, because the bundle cannot carry its own publication facts.
- Commands `desk hub-check`, `desk sync --date | --from --to | --last N`, `desk eod --sync`, `desk import-drop`. Every `desk eod` prints the market snapshot ID, its origin (hub and `published_at` with age, or "local file" with `known_at`).
- Lineage in the run log: `cut_type`, `hub`, `published_at`, `backend_git_sha` next to the snapshot IDs and the core version. The snapshot age is in the run log; the prototype's `window.DESK` meta has no field for it (S5/S6).
- `backend/examples/test_book.csv`, generated by `desk demo-book --profile test`: 40 positions in `OFZ`, `CORP`, `MIXED`, long and short.
- `tests/test_slice_s3.py`: 14 tests against an in-process stub hub, and `pytest -m live` for the real hub (the default run deselects it).

**Not built:** `If-None-Match`/304 in the client. The cache never downloads a bundle it has, so it never needs to ask.

**Run on the cached dev-export dates 9–15 October** (the hub publishes none yet), test book:
- Priced 35 of 40 positions (36 from 14.10). Listed with a reason: OFZ-PK and a key-rate corporate floater (`coupon_floating`, S4), OFZ-IN (`face_indexed`), a perpetual (`perpetual`), and on 9–13.10 a corporate with no quote (`no_quote`; on 14.10 it is priced and gets `not_priced_prev` instead of PnL).
- Coupons paid inside the window show as cash received: CRP-03, CRP-12 and MIX-05 on 12.10, MIX-04 on 14.10, MIX-02 on 15.10.
- The flash PnL identity holds on every position: the largest error is 2.5·10⁻⁴ RUB.

| Date | OFZ | CORP | MIXED | Total, m RUB |
|---|---|---|---|---|
| 12.10 (3 days) | 44.72 | 7.33 | −3.75 | 48.31 |
| 13.10 | 15.54 | 3.35 | −0.34 | 18.55 |
| 14.10 | 20.57 | −0.63 | −5.42 | 14.52 |
| 15.10 | 13.44 | 2.45 | −2.65 | 13.24 |

**Live state on 19 October.** `desk config` reads the hub address and the token file from `backend/.env`. The token is accepted: `/v1/coverage` and `/v1/bonds` answer 200. `desk hub-check` and `pytest -m live` stop at "hub (version 0.1.0) has no snapshot layer: /v1/snapshots answers 404", as expected before H1. The hub's snapshot layer is still only in its local branch, not on GitHub.

**Open for the live acceptance**, once the hub has done H1–H4:
- `desk hub-check`, `desk sync --last 5`, then `desk eod` over those dates with the test book;
- `pytest -m live`;
- compare VPS-published and dev-export snapshots of the same dates (F6);
- regenerate `test_book.csv` from the published dates if the window cases (no quote, coupon inside the window) no longer fall inside it.

## S4 result (19 October 2026)

Built by the orchestrating session with subagents: two research passes (core 0.32 API, snapshot inputs), then five implementation pieces, each checked against a baseline by the session before integration.

**The owner's rule, set during S4.** "A bond is valued somehow rather than not at all." It replaces the S1–S3 habit of rejecting with a reason code. Every valuation carries `quality` (`exact`, `assumed`, `market_value`, `stale_price`, `model_price`, `par_proxy`) and `assumptions`; the ladder is in `backend/README.md`, "Valuation quality". The owner also approved pricing floaters on `inferred` formulas with default conventions, checked against MOEX accrued.

**Universe on 15.10** (4033 active instruments):

| Quality | Before (S3) | S4 |
|---|---|---|
| exact | 1034 | 1032 |
| assumed | – | 869 |
| market_value | – | 863 |
| model_price | – | 159 |
| par_proxy | – | 1110 |
| not valued | 2999 | 0 |

Two bonds moved from exact to assumed (an accrued mismatch or a coupon amount computed from the rate). On 09.10: exact 1028, assumed 863, market value 858, model price 166, par proxy 1118. Par proxies are bonds with no price in the cache and no usable schedule, 96 % of them structured notes with unknown coupons.

**Fixed-coupon bonds through `value_portfolio`.**
- Against S3 on 09–15.10, both books: PV within 0.0004 RUB per position, flash PnL totals identical, YTM and duration identical, the same bonds priced, the validation checks unchanged (ok 502, warn 298, mismatch 77).
- Z moves only on short horizons, up to 4.3 bp on a bond 193 days from its horizon. The cause is the old backend KBD curve (weekly first nodes, off by up to 6.1 bp at the short end against the formula); core's KBD is off by at most 0.003 bp. The brief's "agree to 0.01 bp" therefore holds at every pillar except 1M (0.19–0.31 bp), where the old curve was wrong. `desk.curves.kbd` is deleted.
- **F9:** core 0.32 has no trade-date knob in `ValuationPolicy`. With the default `settlement_lag_days=1` and MOEX accrued (measured to the trade date), Z is off by 0.5–2.8 bp; with `settlement_lag_days=0` it matches to 0.003 bp. The backend builds every instrument with lag 0 (core brief C2).
- DV01 and CS01 differ from S3 by up to 4·10⁻⁴ relative: core bumps centrally, the old code forward.
- Each run stores the hash-verified `rub-portfolio-risk-result.v2` (one per spread kind, core brief C8) under `<dir of --db>/results/`, named with the book's position IDs; the run lineage lists their hashes.

**Curves and fixings** (`curves/rates.py`): RUONIA OIS under `rub_ruonia_ois_moex_v2` and the key-rate projection under `rub_key_rate_projection_v1`, built on every dev date and repricing their quotes to 10⁻¹⁰ bp. The hub's calendars end 2027-12-31, which would cut both curves at 1Y; the backend extends every calendar provisionally to 2079 (weekends and fixed public holidays, TARGET by its own rules) and records `provisional_from` in the lineage (hub brief H16). Quote-set choice: business-date sets, not stale before stale, bid/ask before mid-only, then the latest; `ratescalcs` on 09, 12 (OIS) and 15.10, `otc_monitor` otherwise.

**Floaters** (core 0.26 `FloatingCouponLeg`). Default conventions come from a study of the published coupons since 20.09.2025, reproduced to the kopeck:
- corporate KEY_RATE: arithmetic average shifted 6 calendar days reproduces 2277 of 3755 coupons (single fixing 5 business days before: 1803; compounded: 9);
- corporate RUONIA: the same rule, 247 of 380;
- OFZ-PK: core's Minfin rule, 39 of 44 (the hub's curated `lag_days 7` for SU29015 reproduces 4); the in-advance issues 29007–29010 on their 6-month window; RUONIA_3M and RUONIA_INDEX compounded with a 7-day shift (14/14, 19/20);
- RUONIA_6M (0 of 24 under every candidate), ZCYC, CPI and OTHER go to the market-value rung.

Accrued against MOEX agrees on 454 of 468 corporate floaters, 1004 of 1013 fixed corporates and all OFZ-PD; a mismatch keeps the valuation and flags it. OFZ-PK in-advance issues match MOEX (SU29007: yield 14.781 % vs 14.768 %); in-arrears issues differ by up to 0.6 pp in yield and 50 bp in Z, because MOEX holds the current coupon flat while the backend projects it on the RUONIA OIS curve.

**Perpetuals.** No call dates in the data (hub brief H19), and DEC10 sets no default. The backend assumes a call on the first coupon date at least 5 years after the business date, coupons past the schedule at the current rate, flagged; 18 perpetuals are valued so, called 2031–2032. Five years after issue, the alternative, would call most of them within months and give deep-discount perpetuals yields in the thousands of percent.

**Flash PnL, both ways** (`pnl/compare.py`). Carry, rolldown and rates agree to 2·10⁻⁷ RUB per position, and the totals to 7·10⁻¹¹ relative. The methods differ in where the spread × curve cross term goes: flash measures spread on the D curve (the frame of the published CS01) and has no residual; core measures it on the rolled curve and reports the cross term as `residual` (up to 0.38 m RUB a day on the OFZ demo book). Core's explain also cannot show a schedule change (`other`), exercise assumptions or floaters. **`desk.pnl.flash` stays**; `compare.py` is its regression check (core brief C5).

**Test book, 09–15.10:** 40 of 40 positions valued on every date (exact 35–36, assumed 3, market value 1, model price 1 on 9–13.10). The 35–36 exact positions have the same flash PnL as in S3 to 3·10⁻⁴ RUB.

| Date | Exact | Assumed | Market value | Model price | Total, m RUB |
|---|---|---|---|---|---|
| 12.10 (3 days) | 48.31 | 1.81 | 0.43 | −6.64 | 43.90 |
| 13.10 | 18.55 | 0.96 | 1.38 | 7.95 | 28.85 |
| 14.10 | 9.86 | 1.40 | 0.20 | – | 11.46 |
| 15.10 | 13.24 | −0.12 | −0.09 | – | 13.03 |

On 14.10 the exact column includes −4.66 m RUB from MIX-08, whose first price after three model-price days replaces the model value (simple PnL).

**Review and fixes.** An independent review of the integrated diff found six defects, all fixed with regression tests before this note:
- a MOEX yield of −100 % (RU000A10FSB7 on 09.10) crashed the universe run; such yields now count as missing, and an unexpected error for one bond sends it to `par_proxy` with the error named, instead of stopping the run;
- a bond past maturity kept its value while its redemption was counted as received (PnL ≈ q × face on the maturity day); a matured or fully redeemed bond is now valued 0;
- a stale price was applied to the face of the old quote after an amortization (PV and PnL overstated by the amortized principal); the face is now the one outstanding on the business date;
- PnL was published against a par proxy; a position at par on either date now gets no PnL with reason `par_proxy`, and PnL across a model price carries a flag ("price discovery: model -> market", "model price on both dates");
- the dev market snapshots name a static snapshot of a later date (17.10); the run now warns about it, and the redemption check uses the face on the trade date from the schedule (`redemption_incomplete` 27 → 0 on 09.10);
- `pnl/compare.py` compared fallback rungs; it now takes exact and assumed bonds only, on the schedule the valuation used.

**Open after S4.**
- **Model prices are noisy.** MIX-08 moved ±3 % a day on the model price, because the issuer's traded bonds (and so its median Z) change from day to day. S5: keep a model position's spread from the previous run in the desk store, so only the curve moves it.
- The Desk Monitor has no quality column yet; the flags are in `extra` and the run log (S6).
- FX-face bonds now publish `fx_delta` and so enter the open currency position. The exposure is real; the desk should confirm it wants them there.
- The class median Z for model prices is crude (p90 error 29 % on a back-test); the snapshot has no ratings to bucket by.
- The hub dataset → core spec mapping lives as constants in `curves/rates.py` and the adapter; a versioned table in the interface catalogue is still to write.
- OFZ-IN waits for CPI (hub brief H10); until then they are market-value rungs.
- Timing: `desk eod` of one date with the universe report takes about 13 s, a book run 6–12 s (the model-price medians value the universe once per run).
