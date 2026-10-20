# R14, R15, R17: risk by curve and intraday runs after the 20.10 update

20 October 2026. Owner reports after installing the 20.10 patch (see [OWNER_REQUESTS.md](../../program/OWNER_REQUESTS.md)):

- **R14**: "after the RUONIA update it still shows no risk" on `RUB RUONIA`.
- **R15**: "KEY-rate risk comes from only one bond".
- **R17**: "all runs are on 19.10 although today is 20.10", no intraday run.

We cannot see the owner's data. This note works from the desk code, the fixtures and the hub's source
(`market-data-hub` main @ 79a2daa, release 0.1.6; production runs 0.1.3). Branch `agent/r14` holds the desk-side
fixes. Line numbers are of that branch, unless the file is the hub's.

## R14: no risk on RUB RUONIA

### What the RUONIA curve needs

- Input: one `curve_quote_sets` entry with `dataset = "ruonia_ois"` and `snapshot_date` equal to the business date
  (`desk/curves/rates.py:116-130`). If several match, the desk prefers one the hub does not flag stale, then one
  with bid and ask, then the latest `fetched_at`. In practice that means `ratescalcs` if present, otherwise the
  mid-only `otc_monitor` set ("off-hours mark").
- Build: every quote that has a mid and whose dates fall in the calendar. At least one usable quote is required;
  the static snapshot must carry the `MOEX_FORTS` and `RU_PRODUCTION` calendars (`rates.py:133-160`).
- The hub's shape matches. Dataset names (`ruonia_ois`, `key_rate_swap`), stale-reason keys
  (`<source>/<dataset>: ...`), `quote_basis`, `position` and `*_pct` all agree with `mdhub/snapshot/market.py:268-342`.
  The hub publishes only sets dated D (`market.py:273`). Under `EXPECTED_CURVES` (`market.py:43-53`) a missing set
  counts as a coverage gap, not an error.
- Without the curve, every RUB bond is discounted on the KBD with quality `assumed`, and its DV01 goes on
  `RUB OFZ` (`valuation/bonds.py:124-131`). Nothing lands on `RUB RUONIA` except a report's own DV01.

### Hypothesis tree

1. **The snapshot has no `ruonia_ois` set dated D** (most likely). On the hub:
   - Ratescalcs answered HTTP 502 (hub ROADMAP WEB-8).
   - The real OTC Monitor load has not been verified since the credentials were entered (WEB-8).
   - Both loaders store a snapshot only when its content hash differs from the last stored one, across dates
     (`loaders/swaps.py:159-176`), and date it by `fetched_at` (`swaps.py:127`). An unchanged curve therefore has no
     set dated D. This bites the morning intraday cuts and quiet days.
2. **The set is there but the curve fails to build.** Possible causes: no quote with a mid, all tenors outside the
   calendar, a missing calendar, or a core rejection.
3. **The page shows a run made before the update.** Only the latest positions date is recomputed after an upgrade
   (`autopilot._eod`). A page pinned to an older run, which `desk-nav.js` remembers for 12 hours and refreshes on
   every navigation, keeps its risk on `RUB OFZ`.
4. **The UI hides it.** Ruled out. "DV01 по кривым" sums per curve in thousands of RUB (`desk-monitor.html`
   `rSummary`). The risk tab lists every curve of the run. No filter drops a curve.

### What the code proved

Before this branch, a missing or unbuilt curve was recorded only as `rates.curve_error` inside the lineage JSON
(`orchestrator.with_rates`). The run log had no line for it, and the pages showed `RUB OFZ` risk with no `RUB RUONIA`
row, without saying why. Each bond's `assumptions` carried the note, but nothing aggregated it.

### Fixed (desk)

- `orchestrator.with_rates` (`orchestrator.py:131-171`) now records the following in the run lineage `rates`:
  - `status` per input: `ok`, `missing` (no set dated D: `rates.QuoteSetMissing`), `error`, or `no_discount`
    (the key-rate curve needs RUONIA);
  - `codes` (`curve_missing:ruonia_ois`, `curve_error:ruonia_ois`, `curve_missing:key_rate_swap`,
    `curve_error:key_rate_swap`, `curve_unbuilt:key_rate_swap`, `fixings_error`);
  - `coverage`, the hub's own list of present and missing source series per dataset.

  The key-rate curve is now tried separately, and its error goes to `key_rate_error`.
- Run log (`publish/tdm_run.rates_log`, `tdm_run.py:513-540`):
  - a WARN line per code, for example `curve_missing:ruonia_ois: нет кривой RUONIA в срезе хаба за 15.10 (нет
    котировок RUONIA OIS) — облигации дисконтированы по КБД, риск отнесён на ОФЗ (RUB OFZ)`;
  - an INFO line with the number of positions with risk per curve;
  - an INFO line with floaters by benchmark: how many core valued, and the rung and reason code of the others.
- Pages:
  - compat `riskNotes` (`api/compat.py:49-62`) carries `{code, curve, text, detail}`. It is inferred from the error
    message for runs made before this change. It also includes `run_before_ruonia` for a run made by logic version
    < 4.
  - Desk Monitor shows the notes under "DV01 по кривым" and above the risk tab.
  - A page pinned to an older run now offers "Последний прогон … — показать" (`desk-nav.js`).
- `VALUATION_VERSION` 5, so the latest date is recomputed once after the update.

## R15: KEY risk from only one bond

### What a key-rate floater needs for KEY risk

Core books a floater's projection on `RUB KEY` only when the floater is valued exactly or `assumed`. That path needs
all of the following:

- the key-rate curve, which needs:
  - a `key_rate_swap` set dated D;
  - the RUONIA OIS curve, for discounting;
  - a `KEY_RATE` fixing that agrees with the last decided CBR meeting;
  - a CBR meeting effective after D but not at TOM (`rates.py:162-192`);
- static terms `coupon_type = floating`;
- a hub formula with benchmark `KEY_RATE` and a spread. Without them the bond is rejected with
  `floater_formula_missing` or `floater_formula_incomplete` (`bonds.py:176-180`);
- a close, legal close or waprice on D, else `no_price`. A quote of another day is rejected as `stale_quote`
  (`bonds.py:452-456`, `bonds.py:645`);
- the fixings history. EOD cuts carry 400 days of it. Intraday cuts carry only 7 days (`market.py:66`, `:679`).

Benchmark names match: the hub maps MOEX `RREFKEYR` and "Ключевая ставка" to `KEY_RATE`
(`moex_static.py:1100-1117`), the same code `FLOATER_BENCHMARKS` uses.

### Hypothesis tree

1. **Most key-rate floaters are on a fallback rung** (most likely, given that one bond does have KEY risk, so the
   curve exists). Illiquid corporates have no close on D, so they get `no_price` or `stale_quote` and land on
   `market_value` or `stale_price`. Others lack a formula spread.

   A fallback floater's DV01 was the duration to its next reset, all of it on the discount curve
   (`valuation/fallback.py:389-397` before this change). So it had **no** KEY sensitivity. This is a desk bug: the
   key-rate exposure of the bond disappeared.
2. **The hub classifies the floater as `unknown` or `variable_known`.** This happens when MOEX publishes no
   benchmark (`moex_static.py:1319-1372`). Then there is no formula at all (`bonds_static.py:1063`). The desk values
   such a bond as a fixed bond with assumed coupons, booking a full-duration `RUB RUONIA` DV01 and no KEY. This is
   hub data and needs a hub task.
3. **The key-rate curve is missing** (as in R14, or a CBR decision at TOM). Then **no** bond has KEY risk.
   R15 says one bond does, so this is less likely for the run the owner looked at.

### Fixed (desk)

- A key-rate floater on a fallback rung now books its proxy on two curves, the way core books an exact one
  (`fallback.key_rate_floater_proxy`, `fallback.py:251-262`, used at `:404-408`):
  - the discount curve: −Macaulay × PV;
  - `RUB KEY`: +Macaulay at the horizon and −(time to reset) at the reset.

  Summed over the curves the buckets equal the old single-curve proxy. So total DV01 and any parallel shift are
  unchanged, and only a move of the key rate against RUONIA moves the bond. The split is stated in `assumptions`.
- The run log's floater line tells hypotheses 1 and 2 apart on the owner's data, by giving rung and reason counts
  per benchmark.

## R17: no 20.10 run

### What the code does

- EOD runs exist only for cached `eod_final` dates. The final close of D is published at 08:00 on D+1, so on 20.10
  the newest EOD run can only be 19.10. That part is by design.
- The intraday step reports a line in every cycle once the hub is configured (`autopilot.py:327-328`). The owner's
  14:04 MSK status had only sync, scan and eod, so it came from a build without R11. The patch was delivered later.
  That status also lacks the history step, which the 77a7f4f build reports when `history_days > 0`; confirm whether
  the snippet was complete.
- With R11 the step works like this:
  - from 10:00 MSK on a business day, it asks the hub for the newest `intraday` cut of today once per half-hour slot;
  - from 19:45 it asks for `eod_preliminary` instead;
  - it values today's book on that cut, with position sets carried forward (`store.current_sets(con, today)`,
    `autopilot.py:700`).

  A missing positions report for 20.10 does not stop it. A start-of-day report is valid as of the day before
  anyway. Pages open on the newest run, from `store.CURRENT_RUNS`: today's intraday run until an EOD run of a later
  date exists.

### Cases walked

- **(a)** Intraday cuts of 20.10, positions only as of 19.10: a run on 20.10. This was already covered by
  `test_intraday`.
- **(b)** No intraday cut on the hub: the step says `skipped: no intraday cut of 2026-10-20 on the hub yet (asked the
  hub)`, and the latest run stays 19.10. That is correct. The cause is on the hub.
- **(c)** The final close of 19.10 is published late. If the intraday run was made before it arrived, the run kept
  measuring PnL against an older close until the next cut. After 19:00 no new cut comes, so it kept it all evening.
  **Fixed:** the step now also re-runs when the previous close changes (`autopilot.py:729-733`), and the failure
  signature includes it.
- **Hub corrections (WEB-11).**
  - Sync re-asks the hub for cached wanted dates hourly and fetches the correction. The EOD step re-runs that date
    ("market snapshot changed").
  - Two holes, both **fixed**:
    1. `SnapshotDir.market_id` broke ties between two non-superseded bundles of one cut by id. With a chain A ← B ← C
       where B was never fetched, it could read A. It now orders by `cut_ts`, then `known_at`
       (`marketdata/bundle.py:128-144`).
    2. The history step never reloaded a date it had already loaded, so the bond charts kept the superseded,
       truncated 0.1.3 rows for good. It now lists `superseded_by` (`cache.hub_latest`) and reloads dates whose
       loaded snapshot the hub has since superseded (`history.superseded_dates`, `autopilot.py:549`).

## Tests

- New file `backend/tests/test_rates_inputs_r14.py` (11 tests):
  - codes and risk curve with and without each quote set;
  - error vs gap;
  - run log and `riskNotes` end to end;
  - notes for older runs;
  - the floater proxy;
  - intraday re-run on a late close;
  - a correction of a cached date;
  - the tie-break with a missing link;
  - the history reload.
- The R17 tests fail on the base code.
- The stub hub now honours `supersedes` and `latest=true` as the hub does.
- Backend: 342 passed. Root: 62 OK.

## What the owner's diagnostics (R13) must show

| Question | Evidence in the R13 export | Confirms |
|---|---|---|
| Is RUONIA / KEY missing or failing? | per market snapshot: `curve_quote_sets` coverage (present/missing `source/dataset`), and per run lineage `rates.status` / `rates.codes` | R14 hyp. 1 vs 2; R15 hyp. 3 |
| Which run is on screen? | latest run id, its logic version, `CURRENT_RUNS` per date | R14 hyp. 3 |
| Why are floaters not exact? | per run: valuation quality × reason counts for `coupon_type = floating`, by benchmark (the new INFO line has them) and counts of `unknown`/`variable_known` bonds with a non-empty MOEX benchmark text | R15 hyp. 1 vs 2 |
| Is there risk per curve? | per run: positions with risk on `RUB OFZ` / `RUB RUONIA` / `RUB KEY` (booleans or counts) | R14, R15 |
| Did the intraday step run? | autopilot steps with status and detail (`intraday`: skipped/ok/error text), app version | R17 (a)/(b) |
| Does the hub publish intraday cuts? | count of `intraday` market snapshots per date on the hub list | R17 (b) |

## Hub-side tasks

| # | Task | Acceptance |
|---|---|---|
| HR1 | WEB-8: prove a real `swaps_otc_monitor` and `swaps_ratescalcs` load on a business day; fix the Ratescalcs 502 | `md.curve_snapshot` has `ruonia_ois` and `key_rate_swap` rows from at least one source for every business day; the eod_final cut of that day lists them under `present` |
| HR2 | Store the first fetch of each MSK date even when its content equals the last stored snapshot (dedup per `(source, dataset, snapshot_date)`, not across dates) | an intraday cut at 10:00 on a day whose curve has not changed since the evening before still carries `ruonia_ois`/`key_rate_swap` sets dated that day; no forward fill (the row is a fetch of that day) |
| HR3 | Intraday cuts: `intraday_fixing_lookback_days` long enough for floater coupons in progress (OFZ-PK in-advance window ~6 months, in-arrears RUONIA periods): 400 like EOD, or at least 200 | an intraday cut lists RUONIA and KEY_RATE fixings back to D−200; an OFZ-PK in its coupon period is valued exactly on an intraday cut |
| HR4 | Key-rate floaters without MOEX `COUPON_BENCHMARK`: classify `floating` with a formula from another source (Cbonds, issue documents) instead of `unknown`/`variable_known` | `floater_formulas` covers every bond whose issue terms reference the key rate; count of active key-rate floaters as `unknown` reported in the snapshot's coverage |
| HR5 | WEB-11 historical eod_final cuts: carry `ruonia_ois`/`key_rate_swap` sets for the dates (legacy curve history) or flag them missing in coverage | each re-published cut has the sets, or `coverage.missing` names them (then the desk's flash PnL D−1→D says `discount_curve_changed`, not 0) |
| HR6 | Confirm production (0.1.3) runs the `snapshot_market` intraday rule (every 30 min 10:00–19:00) and the 19:45 preliminary | `/v1/snapshots?kind=market&cut_type=intraday&business_date=<today>` lists cuts during the day |

## Open (desk)

- A run on a cut without the RUONIA curve, measured against a previous day with it (or the other way round), gives
  every exact bond `NoPnl("discount_curve_changed")`. That is honest, but frequent while HR1, HR2 and HR5 are open.
- Fallback rungs for RUONIA floaters on a date without RUONIA keep their proxy on `RUB OFZ`, the discount curve.
- `desk-nav.js` refreshes the 12-hour pin on every navigation, so a pin effectively never expires. The new
  "Последний прогон" button mitigates this; changing the expiry itself is left for the pages' owner.
- Not checked in a browser in this session. Run Playwright over the Desk Monitor risk tab and summary with a run that
  has `riskNotes` after the merge.
