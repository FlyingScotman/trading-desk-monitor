# 05 Frontend integration

The screens in `prototypes/` are **visual mockups**. They define what the tool should show and how it should feel:
- Desk Monitor (8 tabs);
- Widget Board;
- Pivot Studio;
- Scenario & PnL Lab;
- the Bonds view.

They are not the working application, and the calculations inside them are illustrative. Integration is done **by the backend**: the backend exposes a typed desk API and an event stream, and the frontend renders what it receives.

## 1 Principles

1. **The backend is the only data source.** The frontend calls the desk API only. The browser never calls the hub on the VPS; bond and market views go through a backend proxy that reads the snapshot cache.
2. **The frontend never computes a record.** It filters, sums, formats and charts backend fields. The only calculation it runs is the T1 Taylor approximation, labelled ≈ ([03](03_CALCULATION_SPLIT.md)).
3. **Every number shows its provenance:** run (EOD/intraday, cut time, age), coverage (`partial n/N`), valuation source (`computed` / `imported`), estimate vs official.
4. **One definition per metric.** All screens use the same selectors module, so the same filter gives the same total on every screen.

## 2 Desk API (v1 outline)

REST JSON plus an event stream. The OpenAPI is generated from the backend (FastAPI), and TypeScript types are generated from that OpenAPI. Contract owner: the backend ([README interface catalogue](README.md#interface-catalogue)).

| Resource | Methods | Purpose |
|---|---|---|
| `/api/v1/runs` | GET (filter by date, type) | Run list with status, cut, age, coverage, lineage summary |
| `/api/v1/runs/{run_id}` | GET | Run header and full lineage |
| `/api/v1/runs/{run_id}/positions` | GET (fields=…, book, entity) | Per-position facts: valuation, greeks (sparse, factor-indexed), PnL components, funding, OCP contribution, coverage |
| `/api/v1/runs/{run_id}/aggregates` | GET (dims=book,curve,bucket…) | Server-computed cubes for first paint and reconciliation of screen totals |
| `/api/v1/runs/{run_id}/funding` | GET | Funding lines (POS/HTM/CASH/REPO), OCP transfers |
| `/api/v1/runs/{run_id}/pnl` | GET | Flash PnL, explain components, official attachments with revision and coverage, breaks |
| `/api/v1/runs/{run_id}/scenarios` | GET | Stored T3/T2 results for this run, by definition hash, with coverage |
| `/api/v1/scenario-defs` | GET, POST, PUT, DELETE | Preset (read-only), historical (read-only) and per-user/shared custom scenario definitions |
| `/api/v1/scenario-jobs` | POST (definition, base_run_id) → job; GET `{job_id}`; DELETE (cancel) | T2 exact revaluation |
| `/api/v1/carry` | POST (parameters, base_run_id) | Synchronous carry/funding projection ([03](03_CALCULATION_SPLIT.md) §2) |
| `/api/v1/market/bonds`, `/market/bonds/{secid}/history`, `/market/curves`, `/market/fx` | GET | Market views. When a run is selected, served from that run's pinned snapshot bundle, because the hub's live API supports as-known reads only for bond EOD and fixings. Otherwise read through to the hub's latest data |
| `/api/v1/views` | GET, POST, PUT, DELETE | Saved screen layouts, pivot views, widget boards (per user, optionally shared) |
| `/api/v1/export/{resource}` | GET (`format=csv\|xlsx`) | Exports stamped with `run_id`, cut and generation time |
| `/api/v1/events` | GET (Server-Sent Events) | `run.published`, `run.failed`, `job.progress`, `job.done`, `official.attached`, `data.stale` |
| `/health/live`, `/health/ready` | GET | Liveness; readiness (database, latest snapshot age, worker alive) |

Notes:
- **Errors:** one JSON error shape `{code, message, request_id, details}`, with stable `code` values and HTTP status codes used consistently. The request ID is echoed in logs.
- **Pagination:** list resources use `limit` and a cursor, with bounded maximums. Run-scoped position data for ≤2k positions may be returned whole.
- Responses use the shared coverage vocabulary ([01](01_SYSTEM_OVERVIEW.md) §6). Missing values are `null` with a state, never `0`.
- Greeks use a factor dictionary plus integer indices ([03](03_CALCULATION_SPLIT.md) §4).
- ETags on run-scoped resources. A run is immutable, so these responses are cacheable indefinitely; attachments carry their own revision in the ETag.
- SSE must not be buffered by the reverse proxy.

## 3 Frontend structure (target)

| Layer | Content |
|---|---|
| `api/` | Generated TypeScript types and a thin fetch client; SSE subscription |
| `selectors/` | **The** aggregation layer: sums, groupings, coverage propagation, break classification from backend fields, threshold display from backend config |
| `taylor/` | The T1 engine, a port of the Python reference, with parity fixtures |
| `charts/`, `tables/` | ECharts wrappers; a virtualised grid for positions and pivots |
| `screens/` | Desk Monitor, Widget Board, Pivot Studio, Scenario & PnL Lab, Bonds |
| `theme/` | One design system: tokens, **locally bundled fonts**, light/dark |

- **Build:** TypeScript + Vite.
- **Framework: React + TypeScript** (owner, 16 October 2026), with a virtualised grid (AG Grid Community or TanStack Table) and ECharts. The deciding factors were large grids, filter state shared across screens, live updates and the size of the talent pool. Migrate screen by screen; no big-bang rewrite. Migrate screen by screen; no big-bang rewrite.
- **Serving:** the backend (or the reverse proxy) serves the built assets. There are no external CDN or font calls, so the frontend works without internet.

## 4 Transition from the mockups

1. **Compatibility adapter.** A backend endpoint renders the current `window.DESK` shape from the new API data, so the mockups keep working against real runs during the transition. All five mockup screens read `window.DESK`, so it is removed only when the last screen is migrated (ROADMAP M7). It is never extended.
2. **Port order:**
   1. shared selectors and the API client;
   2. Desk Monitor (summary, positions, curve risk, PnL);
   3. Scenario & PnL Lab (T1/T2 flow, carry endpoint);
   4. Pivot Studio;
   5. Widget Board;
   6. Bonds view.
3. **Behaviours the mockups get wrong, which must not be carried over:**
   - missing official PnL treated as zero;
   - two break thresholds;
   - history scaled by the current share under filters;
   - partial stored results labelled as full revaluation;
   - silent defaults (spread 100 bp, key rate 0, market FX copied as CBR FX).

   Details: FE-1…FE-7 in [reviews/2026-10-16_module_review.md](reviews/2026-10-16_module_review.md).

## 4a Interface concepts on real data

The concepts (`prototypes/concepts/`, owner feedback 21.10) run on the demo set and on real runs alike: `lib/real.js` maps `window.DESK` plus `window.DESK_EXTRAS` (`/concepts-data.js`) onto the demo set's shape, so a concept page needs no code for the data source. Rules it keeps:
- absent data is null and named in the header's notes, never a made-up number (ratings, foreign curves' points; the market's sectors, sizes and coupons until the static snapshot's facts are in, R23);
- a floater has no yield: its spread is the discount margin;
- the OFZ curve of the concepts is the basis OFZ − RUONIA (the run's KBD minus RUONIA OIS), its risk the positions' `ofzSpr01` by tenor; risk the run booked on the KBD (a date without RUONIA) is added to RUONIA;
- instrument types: GOV (ОФЗ-ПД, -ПК, -ИН, sovereign FX), SUB, CORP (fixed / floaters), DER (OTC by the report, FX), other; the same in `api/concepts.kind_of` and `real.js`;
- the desk's series over time are the default runs' own sums (risk, MV, PnL; `publish.summary`, one row per run) and the market history (price, yield, G-spread); C11 charts risk and MV per date from each position's run of that date (face, DV01, CS01, basis, MV, DV01 by curve: `/api/v1/history/positions`), with the book filter; only the demo set rebuilds them as today's sensitivities times the face, and the page says which;
- PnL month and year to date are sums of the runs of the period; when the period is partial (positions start later, business days without a run, market history too short) `pnlPeriods` says so, the header's notes explain it and the pages mark MTD/YTD with «*» (`KIT.partial`);
- the market's payment calendar (C08) is aggregated on the server (`/api/v1/calendar/market`, R23): RUB by day × type (ОФЗ-ПД, -ПК, -ИН, corporate fixed / floating, subfederal, other) × kind, the FX bonds of Russian issuers by day × currency × kind; amounts in the face currency, one currency at a time (C08's FX mode has a currency switch and never sums across currencies); the rows behind a cell come from the same endpoint on a drill (`M.marketCal.flows`). `lib/mock.js` builds the same shape from the demo universe, so C08 has one code path;
- the market universe (C09, C13) carries the static snapshot's issuer, group, sector, outstanding face, coupon and frequency; without ratings C09 colours the market map by issuer sector; CBR meetings (C05, C06, C08, C09) come from the market snapshot's `cbr_meetings` (dates ahead, estimated or announced, past decisions and the key rate); consensus and the rate the curve prices are not in the data and the pages say so;
- the data responses are built once per run and store state and cached with ETag and gzip; `desk serve` builds the newest run's ahead (`app.warm_bodies`). At the owner's size (600 positions, 200 runs) the pages are interactive in under 2 s (`tools/tti.js`, numbers in `backend/README.md`).

## 5 Live behaviour (intraday)

- The UI subscribes to `/api/v1/events`. On `run.published` it shows a "newer run available" banner, or auto-switches if the user pinned "follow latest".
- The run badge always shows type, cut time, age, positions basis (opening book vs live feed) and greeks age.
- Polling every 30–60 s is an acceptable fallback if SSE is blocked by the proxy.

## 6 Users and access

- **Few users, on the inside Windows server:** the reverse proxy (IIS or Caddy) does authentication. Windows Integrated Authentication (Kerberos/NTLM) if IT allows it; otherwise named accounts with an established session library.
- **Roles:**
  - `viewer`: read;
  - `trader`: plus custom scenarios and T2 jobs;
  - `admin`: inputs, overrides, configuration.
- **Audit:** who loaded inputs, applied overrides, attached official PnL, and ran jobs.
- User preferences, saved views and custom scenarios live on the server (`/api/v1/views`, `/api/v1/scenario-defs`), not in browser localStorage.
