# ADR-001 Single server inside the work network

Status: accepted (owner, 16 October 2026). Supersedes:
- `docs/program/DECISIONS.md` ADR02, ADR03, ADR05 and ADR06;
- the roles in ADR04 (reader/curator/admin, replaced by viewer/trader/admin in [05](../05_FRONTEND_INTEGRATION.md) §6; ADR04's authentication principles still apply);
- the Windows-artifact part of ADR08 (artifacts target the server);
- in `docs/program/EXECUTION_SPEC.md`:
  - the external-VPS / data-perimeter design (§2–§3, §6–§7);
  - the work-PC "Local Windows backend" (§8);
  - the §11 "Boundary" and API-role gates;
  - the §11 provisional performance targets (replaced by [03](../03_CALCULATION_SPLIT.md) §2, also provisional).

## Context

The execution pack of 14 October assumed three things:
- market data collected on an external VPS;
- portfolio calculations on a Windows work PC;
- an LLM "curator" agent operating the VPS.

The owner has decided that the market-data hub, the desk backend and the frontend will all run on one server inside the work network. The target is a few users and at most about 2k positions.

## Decision

1. **One server inside the work network** hosts:
   - the hub (collector jobs plus its read API, bound to localhost);
   - one PostgreSQL instance with separate databases `mdhub` and `desk`;
   - the desk backend (API, orchestrator, job worker);
   - the frontend static assets, served by the backend or a reverse proxy.
2. **Outbound internet** goes through the corporate proxy to the public sources (MOEX ISS/APIM, CBR, ECB, Ratescalcs). If outbound access fails, a manual file-drop ingest path keeps the hub usable.
3. **No LLM runtime agent** is part of the architecture. Deterministic jobs, quality checks and alerting cover operations. The Hermes material is archived.
4. **Bank-internal sources** become first-class, pluggable sources alongside the public ones (booking system, internal FX mark, any licensed vendor feeds). Each series records its source in the snapshot.
5. **Products core cannot price yet** are imported from bank systems through contract 1.3 CSVs ("hybrid valuation", [01](../01_SYSTEM_OVERVIEW.md) §3).

## Consequences

- The "work PC never sends portfolio data to the VPS" boundary disappears. It is replaced by an ordinary server-security boundary: authentication, roles, localhost-only services, and backups that stay inside the network.
- Development still happens outside the network on public and synthetic data, so the "develop outside, run inside" rules in [06](../06_DEPLOYMENT_AND_OPERATIONS.md) still apply.
- New open items: the server OS (Docker/systemd availability), proxy behaviour (TLS inspection, NTLM), and data licensing for MOEX/Ratescalcs data used inside a bank.
- The hub's existing deploy (docker compose, Caddy, systemd) is reused if the bank allows it. Otherwise it is adapted ([06](../06_DEPLOYMENT_AND_OPERATIONS.md)).
