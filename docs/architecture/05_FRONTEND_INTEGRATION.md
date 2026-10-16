# 05 Frontend integration

The screens in `prototypes/` are **visual mockups**. They define what the tool should show and how it should feel:
- Desk Monitor (8 tabs);
- Widget Board;
- Pivot Studio;
- Scenario & PnL Lab;
- the Bonds view.

They are not the working application, and the calculations inside them are illustrative. Integration is done **by the backend**: the backend exposes a typed desk API and an event stream, and the frontend renders what it receives.

## 1 Principles

1. **The backend is the only data source.** The frontend calls the desk API only. The hub is reachable only from the backend, bound to localhost; bond and market views go through a backend proxy.
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
- **Framework:** React with a virtualised grid (AG Grid Community or TanStack Table) and ECharts is the default recommendation. The deciding factors are large grids, filter state shared across screens, live updates and the size of the talent pool. The decision is open (Q9 in [ROADMAP](ROADMAP.md)) and is needed before M1, because M1 migrates the first screen. Migrate screen by screen; no big-bang rewrite.
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

## 5 Live behaviour (intraday)

- The UI subscribes to `/api/v1/events`. On `run.published` it shows a "newer run available" banner, or auto-switches if the user pinned "follow latest".
- The run badge always shows type, cut time, age, positions basis (opening book vs live feed) and greeks age.
- Polling every 30–60 s is an acceptable fallback if SSE is blocked by the proxy.

## 6 Users and access

- **Few users, on the in-network server:** the reverse proxy does authentication. Windows Integrated Authentication (Kerberos/NTLM) if IT allows it; otherwise named accounts with an established session library.
- **Roles:**
  - `viewer`: read;
  - `trader`: plus custom scenarios and T2 jobs;
  - `admin`: inputs, overrides, configuration.
- **Audit:** who loaded inputs, applied overrides, attached official PnL, and ran jobs.
- User preferences, saved views and custom scenarios live on the server (`/api/v1/views`, `/api/v1/scenario-defs`), not in browser localStorage.
