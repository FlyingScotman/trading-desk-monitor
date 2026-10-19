# Brief for the market-data hub

As of 18 October 2026 (S4 items H16–H25 added 19 October). From the desk system (this repository) to the work on `market-data-server`. Each item has an ID, the reason, and a check. The hub's own `docs/STATUS.md` and `docs/SYSTEM_CONTEXT.md` remain the place where the hub tracks its state; copy the items you accept there.

Sources of the items:
- [ADR-001](../adr/ADR-001-hosting-topology.md) as amended on 18 October: one hub on the VPS serves every consumer;
- the S1/S2 findings and the S3 brief in [VERTICAL_SLICES](../VERTICAL_SLICES.md);
- core 0.32's snapshot inputs (core `docs/portfolio-v3.md`).

## State seen from outside (18 October 2026)

- The VPS hub answers `/health/ready` with `ok`: hub 0.1.0, 23 migrations, none pending.
- Without a token, every existing `/v1` route answers 401, which is correct. `/v1/snapshots` answers 404: the route does not exist in the deployed version.
- **The snapshot layer exists only in the branch `claude/silly-tharp-ed59c7` (commit `ac06c17`) of one development checkout. It is on no GitHub branch.** Another machine or harness cannot see it.
- `docs/SYSTEM_CONTEXT.md` on `main` still describes the 16 October design: an inside instance with a replica mode, the desk as the only consumer, and the backend reading from an inside endpoint. All three were dropped on 18 October.
- `docs/STATUS.md` on `main` names the production address. The repository is public. The desk repository keeps hub addresses out of the repository (the backend reads `DESK_HUB_URL` from its settings); decide whether the hub does the same.

## Blocking the desk (slice S3)

| ID | Item | Check |
|---|---|---|
| H0 | Push `claude/silly-tharp-ed59c7` to GitHub, so the snapshot layer is not on one machine only | The branch is visible on GitHub |
| H1 | Merge the snapshot layer into `main`: contract `schemas/market_snapshot/v1` with fixtures, as-known-at readers (0500), `ops.published_snapshot` (0510), builders, jobs, `/v1/snapshots*`. Renumber its migrations after the current ones; the branch is based on an older `main` (finding F7). Release and deploy | `/health/ready` lists the new migrations; the hub CI rebuilds the golden fixtures byte for byte |
| H2 | Read grant on `ops.published_snapshot` for the API's database role (service roles) | The list call answers 200 |
| H3 | Schedule `snapshot_static` (daily) and `snapshot_market` for `eod_final` and `eod_preliminary`. Cut times come from `MDHUB_SNAPSHOT_*` until DEC04 | A new `eod_final` appears after every business day |
| H4 | Publish `eod_final` once for the last ten business dates. They carry today's `known_at` (F6); this is accepted for testing | `GET /v1/snapshots?kind=market&cut_type=eod_final` lists ten dates |
| H5 | Named tokens per consumer, each with the narrowest scope: `desk-dev` (`snapshots`) for the backend on a development machine; `dev-read` (`read`) for checks. This can be done now, before H1 | `/v1/coverage` answers 200 with `dev-read` |

## Documentation

| ID | Item |
|---|---|
| H6 | Rewrite `docs/SYSTEM_CONTEXT.md` to match ADR-001 as amended on 18 October. Points: one VPS hub; several consumers with scoped tokens; no inside instance and no replica mode (drop work item 7); the backend pulls snapshots into its own cache, with a drop folder as fallback; inside-only data never enters the hub |

## Data quality found by the desk (S1, S2)

| ID | Item | Evidence |
|---|---|---|
| H7 | Publish MOEX accrued interest (`accrued_per_bond`) for **every** instrument of the snapshot, from the securities board data, not only for instruments that traded | F3: on 15.10, 314 instruments have a quote without accrued and 130 have no price |
| H8 | Bond static verification: `known` coupon amounts that contradict MOEX accrued | F4: RU000A101M04, next coupon 30.21 RUB against accrued 36.45 RUB |
| H9 | Only if S4 still finds mismatches after moving to core's rate basis: record per instrument the accrual basis MOEX uses, or state in the contract that it is not published. Today every instrument carries `day_count_not_published` | F3, F9: 1014 of 7675 quotes match neither the accrued to the trade date nor to T+1 under a plain day count; core 0.17's rate basis matches MOEX on 18 golden bonds |

## Inputs core 0.32 can use and the snapshot does not carry yet (slices S4, S8)

Core has engines for these products. The desk can only compute them when the market snapshot carries their inputs (contract v1.x; a new dataset bumps the minor schema version). Order by the desk's need: H10 first (OFZ-IN in S4), then the S8 items.

| ID | Dataset | For | Hub source today |
|---|---|---|---|
| H10 | CPI series, point in time, with publication dates | OFZ-IN (core `linkers`, 3-month lag) | Rosstat source exists (migration 0425); not in the snapshot |
| H11 | Futures settlement prices and margined prices per contract | FX, RUONIA, key-rate and OFZ futures (core `fx_futures`, `rate_futures`, `ofz_futures`) | FORTS EOD and contract static (`/v1/futures/*`); not in the snapshot |
| H12 | RUONIA index values, and RUONIA fixings with declared unpublished days | RUONIA index futures; rate futures reject an undeclared gap | Fixings are in the snapshot; the index and the gap declaration are not |
| H13 | FX spots with value date and settlement calendar (TOM), swap points or outrights | FX forwards and swaps, XCCY (core `fx_forwards`, `xccy`) | `fx_rates` has CBR and MOEX values without value dates |
| H14 | Foreign overnight fixings (SOFR, €STR, CNY index) as needed | Floating foreign legs of XCCY | Not collected |
| H15 | CDS par spreads or upfronts with recovery | CDS (core `cds`) | **No source.** Until one exists, CDS stays `imported` |

## Found in slice S4 (19 October 2026)

Evidence is from the dev snapshots of 09–15.10. Until each item lands, the backend works around it and flags the affected positions (the owner's rule: a bond is valued somehow rather than not at all).

| ID | Item | Evidence and the backend's workaround |
|---|---|---|
| H16 | **Calendars to at least 2037**, rule-based beyond the official lists, with `provisional_from` | Every calendar ends 2027-12-31, so the RUONIA OIS and key-rate curves lose 2Y–10Y (core refuses dates outside a calendar). The backend extends calendars provisionally (weekends and fixed public holidays) and records it in the lineage |
| H17 | **Publish the SPFI (MOEX derivatives settlement) and RUONIA publication calendars** explicitly, and give the fixture calendars the same history as the snapshots | MOEX OIS RUONIA uses the joint SPFI + Moscow + RUONIA calendar. The backend maps SPFI to `MOEX_FORTS` and RUONIA to `RU_PRODUCTION` plus year-end days. `RU_PRODUCTION` follows the observed RUONIA days in past years, which is wrong for Moscow business days. The vendored fixture calendars start 2026-01-01, shorter than the 400-day fixing lookback |
| H18 | **Floater conventions from the issue terms** (`status: curated`): fixing lag or lookback, averaging or compounding, fixing day, floor, cap, multiplier | 644 of 647 formulas are `inferred`; `lag_days` is set on 3, `averaging` on 1, floor/cap/multiplier/fixing day on none. The backend prices them on default conventions, checks against MOEX accrued, and flags "formula inferred" |
| H19 | **Call schedules of perpetuals** (and of callables generally) | All 62 perpetuals carry `perpetual_no_call_date`; none has a scheduled call offer. The backend assumes a call date per DEC10 and flags it |
| H20 | **Fixings with a publication time** (`published_at`), for every index, and dev snapshots that are point in time | Only RUONIA has `published_on`, a date without a time; `published_at` is always null. The dev snapshots carry `known_at` 2026-10-17 and rows dated after their business date (F6). The backend counts a same-day publication as not yet known |
| H21 | **EOD swap marks with bid and ask** for RUONIA OIS and key-rate swaps | `ratescalcs` is a 09:00Z mark and is missing on 12–14.10; `otc_monitor` is mid-only and flagged stale "off-hours mark". On 15.10 the two sets have identical mids: check whether they are one feed. The backend prefers a set not flagged stale, then bid/ask, then the latest, and records the choice |
| H22 | **Units of `RUSFARIND`** | Labelled `pct`, value 2521.53: looks like an index level |
| H23 | **`known` coupons without an amount** | 6164 future `known` periods have a rate but no amount, mostly MBS and securitizations; RU000A1043Q6, RU000A1043S2 and RU000A10FEC5 end with a one-day 0.01 % stub without an amount. The backend computes the amount from the rate (RATE_ACT_365) and flags it |
| H24 | **Price basis of amortized MBS** | RU000A0JV755 is quoted at 474 % of its current face of 48.64 RUB: the price looks like a percentage of the original face. The backend uses it as given |
| H25 | **Static snapshots point in time, and the face on the static's own date** | Every dev market snapshot of 09–15.10 names a static snapshot of 17.10 (F6). On the dev data a static face still includes redemptions dated on the static's own date, and MOEX keeps the pre-final face on matured bonds (180 of 181). The backend now derives the face on the trade date from the redemption schedule and relies on that rule; please confirm it, or state the rule in the contract. If a same-day static already excludes them, payment-day amortizers drop from exact to market value |

## Not hub work

- Inside-only data: the internal 19:00 FX mark, bank feeds, trader marks, positions, official PnL. It enters through the backend's private-input adapters and never reaches the VPS.
- Conventions: day counts, spot lags, the curve-spec registry. These belong to core; the hub publishes facts and codes ([02](../02_MARKET_DATA_AND_CORE_CONTRACT.md)).
