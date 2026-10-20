# Desk frontend

The React screens of the Trading Desk Monitor (Vite 5, React 18, TypeScript, vitest). Slice S6 built the first one, the **Scenario Lab**: pick a run and a scenario, read the Taylor estimate (≈) computed in the browser from the run's stored risk, then ask the backend for the exact repricing and its residual. The other screens are still the prototypes in `prototypes/`, fed by `window.DESK`. Design and results: [`docs/architecture/VERTICAL_SLICES.md`](../docs/architecture/VERTICAL_SLICES.md), S6.

The app shares the navigation bar of the prototype pages: `index.html` loads `../desk-nav.js` (served by `desk serve` next to `/app/`; the dev server proxies it too) before the app, which puts the run chosen for all pages (`?run_id=` / `?as_of=`, remembered in localStorage) into the address; the screen reads it from there and switches runs through the bar. The build warns that this classic script "can't be bundled without type=module": it is meant to stay as it is.

`desk serve` serves the built app at `/app/` from `frontend/dist`. `dist` is committed on purpose: the inside server has no Node, so running the app needs none. Upgrade: CI builds it into the release.

## Develop

Requirements: Node 18 or later and npm. Then `npm ci` here.

Run the backend on a free port, then the Vite dev server, which proxies `/api` to it:

```bash
cd backend && .venv/bin/desk serve --port 8767
cd frontend && DESK_API=http://127.0.0.1:8767 npm run dev
```

Open http://localhost:5173/app/. Without `DESK_API` the proxy goes to `http://127.0.0.1:8000`.

## Commands

| Command | What |
|---|---|
| `npm test` | vitest, including the Taylor parity test |
| `npm run typecheck` | `tsc --noEmit` |
| `npm run build` | type check, then build into `dist/` (commit the result) |
| `npm run gen:api` | regenerate `src/api/schema.d.ts` from `openapi.json` |

`openapi.json` is the desk API's OpenAPI document. After an API change, refresh it with `cd backend && .venv/bin/desk openapi --out ../frontend/openapi.json`, then run `npm run gen:api`. A backend test fails when the committed `openapi.json` is stale.

## Taylor parity

`src/taylor/taylor.ts` is a port of the Python reference `desk.scenario.taylor`. `src/taylor/taylor.test.ts` checks it against the backend fixture [`backend/tests/fixtures/taylor_parity.json`](../backend/tests/fixtures/taylor_parity.json) to 1e-9 relative, and fails if the fixture is missing. Regenerate the fixture on the backend with `DESK_REGEN_FIXTURES=1 pytest tests/test_slice_s6.py -k parity`.

Numbers computed in the browser are never persisted.
