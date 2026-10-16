# ADR-001 Desk inside the work network on Windows; the hub may span a Linux VPS and the inside server

Status: accepted (owner, 16 October 2026; hosting detail amended the same day). The hub split between VPS and inside remains open.

**Supersedes:**
- `docs/program/DECISIONS.md`:
  - ADR03 (work-PC download of a broad VPS bundle), which is replaced by snapshot replication (below);
  - ADR05 (Hermes runtime);
  - ADR06 (localhost on a work PC);
  - the roles in ADR04 (reader/curator/admin, replaced by viewer/trader/admin in [05](../05_FRONTEND_INTEGRATION.md) §6). ADR04's authentication principles still apply.
- `docs/program/EXECUTION_SPEC.md`:
  - the work-PC-only "Local Windows backend" (§8), which now targets a Windows server;
  - the Hermes runtime (§7);
  - the §11 API-role gate;
  - the §11 provisional performance targets (replaced by [03](../03_CALCULATION_SPLIT.md) §2, also provisional).

ADR02 (Linux VPS with PostgreSQL) still applies to the VPS part of the hub. ADR08 (Linux and Windows release artifacts) still applies.

## Context

The execution pack of 14 October assumed:
- market data collected on an external Linux VPS;
- portfolio calculations on a Windows work PC;
- an LLM "curator" agent operating the VPS.

The owner has since decided:
1. The **desk backend and frontend run on a Windows server inside the work network.** That machine has Python 3.11.
2. The **market-data hub may be split** between a Linux VPS outside and the Windows server inside. The exact split is not final.
3. All market data is properly licensed.
4. Target scale is a few users and at most about 2k positions, with intraday refresh.

## Decision

1. **Inside (Windows server):**
   - the desk backend (API, orchestrator, job worker);
   - the frontend static assets;
   - PostgreSQL with the `desk` database;
   - optionally an inside hub instance with its own `mdhub` database.

   Services run as Windows services.
2. **Outside (Linux VPS):** the hub's public collectors and its snapshot publisher, using the existing hub deploy (docker compose / systemd). **No private data ever goes to the VPS.**
3. **The unit that crosses the boundary is the immutable snapshot** ([ADR-002](ADR-002-materialized-market-snapshots.md)).
   - Whichever hub instance collects a dataset publishes snapshots.
   - The inside side pulls VPS snapshots over HTTPS through the corporate proxy and verifies their sha256.
   - The backend always reads snapshots from the inside endpoint (a local hub instance or a local snapshot cache). It never depends on live VPS availability for a run.
4. **Where each collector runs** is decided per source, not per system:

   | Collector kind | Runs |
   |---|---|
   | Sources reachable only from inside (bank-internal feeds, the internal FX mark, any vendor feed licensed to the bank network) | Inside |
   | Public sources the proxy blocks or throttles | VPS |
   | Everything else | Either; prefer one authoritative location per series |

5. **No LLM runtime agent** is part of the architecture. Deterministic jobs, quality checks and alerting cover operations.
6. **Products core cannot price yet** are imported from bank systems through contract 1.3 CSVs ("hybrid valuation", [01](../01_SYSTEM_OVERVIEW.md) §3).

## Consequences

- **Hub on two hosts.** The hub must run on both Linux and Windows, and must support a "replica" mode: ingest published snapshots from another instance, verify them, and serve them unchanged. Snapshot IDs are content hashes, so replication is idempotent.
- **Python version.**
  - The Windows side is pinned to Python 3.11. Core (≥3.11) fits.
  - The hub currently requires ≥3.12. Its Windows part either lowers the requirement to 3.11 or ships its own embedded 3.12 runtime.
  - The VPS keeps 3.12.
- **Development.** It happens outside on public and synthetic data ("develop outside, run inside", [06](../06_DEPLOYMENT_AND_OPERATIONS.md)).
- **Open items:**
  - proxy behaviour (TLS inspection, authentication);
  - the final collector placement;
  - Windows service tooling.
