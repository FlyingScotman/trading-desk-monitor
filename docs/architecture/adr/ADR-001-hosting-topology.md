# ADR-001 Desk inside the work network on Windows; one hub on a Linux VPS serves every consumer

Status: accepted (owner, 16 October 2026; hosting detail amended the same day). **Amended 18 October 2026:** the hub runs as one instance on a Linux VPS and serves several consumers. The split of the hub between the VPS and an inside instance, left open on 16 October, is dropped (see History).

**Supersedes:**
- `docs/program/DECISIONS.md`:
  - ADR03 (work-PC download of a broad VPS bundle), which is replaced by the backend's snapshot cache (below);
  - ADR05 (Hermes runtime);
  - ADR06 (localhost on a work PC);
  - the roles in ADR04 (reader/curator/admin, replaced by viewer/trader/admin in [05](../05_FRONTEND_INTEGRATION.md) §6). ADR04's authentication principles still apply.
- `docs/program/EXECUTION_SPEC.md`:
  - the work-PC-only "Local Windows backend" (§8), which now targets a Windows server;
  - the Hermes runtime (§7);
  - the §11 API-role gate;
  - the §11 provisional performance targets (replaced by [03](../03_CALCULATION_SPLIT.md) §2, also provisional).

ADR02 (Linux VPS with PostgreSQL) applies to the hub. ADR08 (Linux and Windows release artifacts) still applies.

## Context

The execution pack of 14 October assumed:
- market data collected on an external Linux VPS;
- portfolio calculations on a Windows work PC;
- an LLM "curator" agent operating the VPS.

The owner has since decided:
1. The **desk backend and frontend run on a Windows server inside the work network.** That machine has Python 3.11.
2. The **market-data hub runs on a Linux VPS** and must be usable from several places, not only from the desk: the desk backend inside the work network, the owner's development machines, and other tools of the owner (18 October 2026).
3. All market data is properly licensed.
4. Target scale is a few users and at most about 2k positions, with intraday refresh.

The hub already has what a shared, internet-facing service needs (`market-data-server/deploy/README_DEPLOY.md`):
- a docker compose deploy with PostgreSQL bound to localhost and a TLS reverse proxy (Caddy);
- named bearer tokens per consumer with scopes (`read`, `snapshots`, `export`), revocable;
- a request log;
- an admin console behind an IP allow-list.

## Decision

1. **Outside (Linux VPS): one hub instance.**
   - It runs every public collector (MOEX ISS/APIM, CBR, ECB, Rosstat, Ratescalcs, OTC Monitor and the rest), the quality and backfill jobs, and the snapshot publisher ([ADR-002](ADR-002-materialized-market-snapshots.md)).
   - It is the single authoritative location for every series it collects.
   - It uses the hub's existing deploy (docker compose, Python 3.12, PostgreSQL 16).
   - **No private data ever goes to the VPS.** Consumers only read from the hub; no consumer writes to it.
2. **Access for many consumers.**
   - Every consumer gets its own named token with the narrowest scope it needs. The desk backend gets `snapshots`. A development machine gets `read` or `snapshots`.
   - Access is HTTPS only, through the reverse proxy, to `/v1/*` and `/health/*`.
   - PostgreSQL is never exposed. The read-only role is reachable only over an SSH tunnel, for administration.
   - The admin console sits behind the IP allow-list.
   - Tokens go only to the owner's own consumers, within the licence scope of each source.
3. **Inside (Windows server):**
   - the desk backend (API, orchestrator, job worker);
   - the frontend static assets;
   - PostgreSQL with the `desk` database only;
   - a **snapshot cache** in the backend.

   Services run as Windows services. There is no hub instance inside.
4. **The unit that crosses the boundary is the immutable snapshot** ([ADR-002](ADR-002-materialized-market-snapshots.md)).
   - The backend pulls snapshots from the hub's `/v1/snapshots`, `/v1/snapshots/{id}/manifest` and `/v1/snapshots/{id}/bundle` endpoints over HTTPS through the corporate proxy.
   - It verifies the sha256 against the snapshot ID and keeps the bundle in its cache.
   - A run reads only from the cache, so it never depends on live VPS availability. The age of the snapshot used is shown.
   - **Fallback when the proxy blocks the VPS:** an operator copies bundle files into the cache's drop folder. They get the same verification. The backend's `SnapshotDir` already reads such a folder (slice S1).
5. **Inside-only data is not hub data.**
   - The internal 19:00 FX mark, bank market-data feeds, trader marks, positions and official PnL enter through the backend's adapters for private inputs ([04](../04_BACKEND.md)).
   - They are applied as versioned overrides on top of a hub snapshot, and never sent to the hub.
6. **No LLM runtime agent** is part of the architecture. Deterministic jobs, quality checks and alerting cover operations.
7. **Products core cannot price yet** are imported from bank systems through contract 1.3 CSVs ("hybrid valuation", [01](../01_SYSTEM_OVERVIEW.md) §3).

## Consequences

- **The hub stays Linux-only.** No replica mode, no Windows port, no Python 3.11 requirement for the hub. The VPS keeps Python 3.12.
- **Python on the Windows side** is pinned to 3.11. Core (≥3.11) and the desk backend fit.
- **The hub's snapshot endpoints are on the desk's critical path.** The snapshot layer must be merged and deployed before the backend can switch from snapshot files to the HTTP client (slice S4, [VERTICAL_SLICES](../VERTICAL_SLICES.md)).
- **One outside dependency for the desk.** A VPS outage delays new snapshots but never blocks a run on cached ones. Alerting covers a snapshot not published at the cut ([06](../06_DEPLOYMENT_AND_OPERATIONS.md) §9).
- **The VPS is internet-facing.** It needs a firewall (22 and 443 only), TLS, token rotation, off-site backups and monitoring.
- **VPS location.** Some Russian public sources (cbr.ru, Rosstat, MOEX) restrict foreign IP addresses at times. Sources with an IP allow-list (Cbonds) need the VPS address registered. Choose the provider and region with the hub's source probes, before the seed restore.
- **Development.** It happens outside on public and synthetic data ("develop outside, run inside", [06](../06_DEPLOYMENT_AND_OPERATIONS.md)). Development machines read the same VPS hub with their own tokens.
- **Open items:**
  - proxy behaviour: TLS inspection, authentication, the allowlist entry for the hub's hostname (Q4);
  - Windows service tooling.

## History

- 16 October 2026: the hub "may be split" between the VPS and an inside instance that replicates VPS snapshots and runs inside-only collectors.
- 18 October 2026: the owner chose one hub on the VPS for all consumers. Inside-only data moved to the backend's private-input adapters; the inside replica and the hub's Windows part were dropped.
