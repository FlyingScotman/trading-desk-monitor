# 05 Frontend integration

The screens in `prototypes/` are **visual mockups**. They define what the tool should show and how it should feel:
- Desk Monitor (8 tabs);
- Widget Board;
- Pivot Studio;
- Scenario & PnL Lab;
- the Bonds view.

They are not the working application, and the calculations inside them are illustrative. Integration is done **by the backend**: the backend exposes a typed desk API and an event stream, and the frontend renders what it receives.

**Deprecated in the UI (22 October 2026).** The main set of screens is now the monitors (`prototypes/concepts/`, C01–C13, §4a), with the React scenario screen (`/app/`) and the settings. The mockup screens above (Desk Monitor, Widget Board, Pivot Studio, Scenario & PnL Lab) are no longer linked from the navigation bar, the home page or the monitors; their files and tests stay, and they open by direct URL. The port order in §4 predates this. Which monitor replaces which: root `README.md`, «Устаревшие экраны».

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
| `/api/v1/runs/{run_id}/funding` | GET | Funding of the run (B1, built): state (`computed`, `not_configured`, …), totals, by book with the write-off, by entity, lines (POS/HTM/CASH/ENTITY_CASH/REPO), problems, the policy and its provisional fields; OCP transfers later (B2) |
| `/api/v1/runs/{run_id}/pnl` | GET | Flash PnL, explain components, official attachments with revision and coverage, breaks |
| `/api/v1/runs/{run_id}/scenarios` | GET | Stored T3/T2 results for this run, by definition hash, with coverage |
| `/api/v1/scenario-defs` | GET, POST, PUT, DELETE | Preset (read-only), historical (read-only) and per-user/shared custom scenario definitions |
| `/api/v1/scenario-jobs` | POST (definition, base_run_id) → job; GET `{job_id}`; DELETE (cancel) | T2 exact revaluation |
| `/api/v1/carry` | POST (parameters, base_run_id) | Synchronous carry/funding projection ([03](03_CALCULATION_SPLIT.md) §2) |
| `/api/v1/market/bonds`, `/market/bonds/{secid}/history`, `/market/curves`, `/market/fx` | GET | Market views. When a run is selected, served from that run's pinned snapshot bundle, because the hub's live API supports as-known reads only for bond EOD and fixings. Otherwise read through to the hub's latest data |
| `/api/v1/views` | GET, POST, PUT, DELETE | Saved screen layouts, pivot views, widget boards (per user, optionally shared) |
| `/api/v1/export/{resource}` | GET (`format=csv\|xlsx`) | Exports stamped with `run_id`, cut and generation time |
| `/api/v1/events` | GET (JSON pages since S7b; Server-Sent Events: #28) | The `event` table in `event_id` order: inbox status changes (`input.accepted`, `input.rejected`, `input.stale`, …, the dataset tells official PnL from positions), `run.published`, `run.failed`; later `job.progress`, `job.done`. `after` = the cursor (the SSE stream resumes from `Last-Event-ID` with it), `before`, `on`, `type` prefixes; each event has a `level` and a Russian `text` (`desk.daystatus`) |
| `/api/v1/inbox`, `/api/v1/inbox/days` | GET (every user, viewers included: #25) | Day status (S7b): per date the inputs, hub closes, files with rejections and their reasons, runs and what superseded what, per book reconciliation; a line per date for the strip. The monitors' C14 |
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

### 4b Market history on the pages (R24)

The market's past comes from the store's market history, which the hub's history API fills years deep (bonds from 2024-01-03, KBD, curves, fixings and FX further back; [04](04_BACKEND.md) §9), else the snapshots. It is not in `/concepts-data.js`, which every page loads; two lazily loaded endpoints carry it, so a page that does not chart it stays as fast as before (pages load in about 0.5 s warm on the fixture server, the market endpoint answers in about 0.2 s and is cached per run until the history changes):
- `/api/v1/history/positions?run_id=&ids=&from=`: a position's price, yield (none for a floater), G-spread, MOEX's Z-spread to the KBD, the floater's discount margin by the runs, a linker's ICPI / IR / BEI, the KBD at its term, face and DV01 by the runs. The card of a position asks for its whole history (the chart opens on the last year; wheel and slider go back), pages charting every position for a year;
- `/api/v1/history/market?run_id=`: per bond the yield and price change over 1D / 1W / 1M / 3M / YTD (C09's movers, any period), КС, RUONIA, RUSFAR, RUSFAR CNY and CPI year on year, CBR and MOEX FX with their changes (C09's strip, C05's market section), RUONIA OIS, key-rate swaps and the KBD at the pillars on those days and two years of 1Y–10Y (C09's curves: compared with a chosen past day, and their history).

`lib/real.js` exposes them as `M.posHistory(id, {deep: true})` and `M.marketHistory()` (synchronous, once); the demo set has neither, and pages fall back to what they showed before.

## 5 Live behaviour (intraday)

- The UI subscribes to `/api/v1/events`. On `run.published` it shows a "newer run available" banner, or auto-switches if the user pinned "follow latest".
- The run badge always shows type, cut time, age, positions basis (opening book vs live feed) and greeks age.
- Polling every 30–60 s is an acceptable fallback if SSE is blocked by the proxy.

## 6 Users and access

- **Few users, sign-in in the app** (DECISIONS DEC13, batch 1 item 9; `backend/src/desk/api/auth.py`). The plan of a reverse proxy (IIS or Caddy, Windows Integrated Authentication) stays an option; Windows / AD single sign-on is a later step that would map AD groups to the same roles.
  - **Off by default:** `desk serve` listens on 127.0.0.1 only, no accounts; the admin routes answer loopback clients with a loopback `Host` and `Origin` (as before).
  - **On** (setting `auth`; `desk lan enable`): every page, script and API needs a session, except `/health/live`, `/login.html` and `/api/v1/auth/{state,login,setup}`. A page without a session is redirected (303) to `/login.html?next=…`, an API call answers 401 (`ErrorOut.code` `unauthorized`); a refused role answers 403 (`forbidden`), a lockout 429 (`locked`).
  - **Accounts:** table `app_user` (migration 0015), Argon2id hashes (argon2-cffi), roles below. The first admin: `desk user add NAME --role admin`, or the setup form of the login page, open only to a loopback client while no active admin exists. Admins manage users at `/users.html` (`prototypes/desk-users.js`, a self-contained section the settings page can host) or with `desk user …`; a reset password is temporary (changed at the next sign-in).
  - **Sessions:** `app_session` keeps the SHA-256 of a random token; the token is in an `httpOnly; SameSite=Strict` cookie (`Secure` over HTTPS). Limits: 8 h idle, 24 h absolute (`session_idle_minutes`, `session_max_hours`). Logout, disabling a user, a role change and a password reset revoke sessions.
  - **CSRF:** a state-changing request needs `X-CSRF-Token` equal to the session's token (the pages read it from the `desk_csrf` cookie: `prototypes/desk-auth.js` adds it to every same-site `fetch`/XHR write; `frontend/src/api/client.ts` too) and a same-site `Origin`/`Referer` when the browser sends one. Login and setup (no session yet) need JSON and a same-site `Origin`.
  - **Throttling:** 5 wrong passwords in a row lock the account for 15 min (`login_max_failures`, `login_lockout_minutes`); 30 failures from one address in that window answer 429 before any check.
  - **Network:** `bind_host` / `port` (default 127.0.0.1:8100). A non-loopback address is refused without `auth`, and without TLS (`tls_cert`/`tls_key`, or `desk lan enable --self-signed`) unless `allow_plain_http`, which puts a warning on every page. Beside a specific address the server also listens on 127.0.0.1, so the launcher's health check stays there. `admin_from_network` (default on) lets admins use the admin routes from other computers. Windows: `desk.bat firewall add` (`netsh advfirewall firewall add rule … dir=in action=allow protocol=TCP localport=<port> profile=domain,private`, administrator). Running as a Windows service (no logged-in user) is a follow-up.
- **Roles** (`auth.ROLES`):
  - `viewer`: read every page and API but the admin routes;
  - `trader`: plus scenario jobs (`POST /api/v1/scenario-jobs`), the autopilot's run now (`POST /api/v1/autopilot/run`) and alias edits (`PUT /api/v1/instruments/alias`);
  - `admin`: plus settings, adapter, updates, diagnostics, users and the audit log (`/api/v1/settings*`, `/api/v1/adapter*`, `/api/v1/update*`, `/api/v1/diagnostics`, `/api/v1/users*`, `/api/v1/audit`).
- **Audit** (`audit_log`, append-only, `GET /api/v1/audit`): logins and failures, logouts, user changes, settings / adapter / update writes (the names of the keys changed, never values or secrets), alias edits, scenario jobs, autopilot runs; with the user (`local` while sign-in is off), the client address and the outcome. Still to come: input loads, overrides and official PnL attachments.
- User preferences, saved views and custom scenarios live on the server (`/api/v1/views`, `/api/v1/scenario-defs`), not in browser localStorage.
