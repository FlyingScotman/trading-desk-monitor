# 06 Deployment and operations

Decision: [ADR-001](adr/ADR-001-single-in-network-server.md). Everything runs on one server inside the work network.

## 1 Server layout

| Process | Bind | Notes |
|---|---|---|
| Reverse proxy (Nginx, Caddy or IIS) | Internal HTTPS port | Authentication, TLS, static frontend assets, SSE without buffering |
| Desk backend API | localhost | FastAPI/Uvicorn |
| Desk orchestrator and T2 worker | — | Scheduled pipelines plus one job-worker process (process pool inside for valuation) |
| Hub API | **localhost only** | Only the backend calls it |
| Hub scheduler (collectors, quality, backfill, maintenance) | — | Existing APScheduler process |
| PostgreSQL 16 | localhost / socket | Databases `mdhub` and `desk`, separate roles; no cross-database joins |

**Sizing envelope** (to be measured): 4–8 vCPU, 16 GB RAM, SSD sized for hub raw captures plus intraday snapshots. A process pool for valuation is the main CPU consumer.

## 2 Open infrastructure question: server OS

The hub already ships docker compose and systemd deployments (`market-data-server/deploy/`). Ask IT:

| Question | If yes | If no |
|---|---|---|
| Linux server allowed? | Reuse systemd units; Docker optional | Windows Server: run services as Windows services (NSSM or WinSW), PostgreSQL Windows build, IIS or Caddy as the proxy |
| Docker allowed? | Images built outside, transferred as archives | Python virtualenvs from an offline wheelhouse |
| Outbound HTTPS through a proxy? | §3 | Manual file-drop ingest for all public sources |

This is open question Q2 in [ROADMAP](ROADMAP.md).

## 3 Corporate proxy and outbound access

The hub needs outbound HTTPS to these hosts:
- `iss.moex.com`
- `apim.moex.com`
- `www.cbr.ru`
- `cbr.ru` (XML/SOAP services)
- `data-api.ecb.europa.eu`
- Ratescalcs
- Cbonds, if licensed

Expect:
- **TLS inspection.** Install the bank root CA into the service trust store (`SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`, or the httpx `verify` setting). **Never disable certificate verification.**
- **Proxy authentication.** NTLM/Kerberos proxies may need a local helper (px, cntlm) or a service account allowlisted by host.
- **An allowlist request.** Submit the host list above with the expected request rates (the hub limits ISS to 3 requests per second).
- **A fallback.** A drop folder where an operator can place source files (CBR XML, MOEX CSV) for the hub to ingest with the same parsers and raw-capture archiving. Staleness is shown in the UI until fresh data arrives.

## 4 Data licensing and internal sources

- **Licensing:**
  - MOEX ISS data, even 15-minute delayed, used inside a bank for valuation and display is typically a licensed use (non-display and display categories in the MOEX market-data policy).
  - Ratescalcs and Cbonds have their own terms.
  - Confirm with compliance before production use. Record the result per source in the hub's source registry.
- **Internal sources:** the bank may already have better data (QUIK, a vendor terminal feed, the internal market-data system, the treasury 19:00 FX mark). The hub's source model is pluggable per series. Add internal sources as collectors with the same raw-capture, quality and snapshot rules, and record the authoritative source per series (DEC04).

## 5 Develop outside, run inside

| Rule | How |
|---|---|
| No real positions, results or internal files leave the network | Development uses public data and synthetic portfolios only |
| Fixtures are public or synthetic | A CI check rejects private identifiers (book names, counterparty names, internal IDs) in `fixtures/` and test data |
| Reproducible installs without internet | Build an offline wheelhouse (or Docker images) outside, transfer it with checksums, install pinned versions. Python version pinned per environment (core needs ≥3.11, the hub ≥3.12, so use 3.12 for both) |
| Versioned releases | Core released with tags; backend pins the exact core version; hub schema version pinned in the backend |
| Code transfer | A git bundle or internal mirror. The canonical remote is open question Q5 in [ROADMAP](ROADMAP.md) |

## 6 Time

- Store instants as `timestamptz` in UTC. The business date is Moscow (UTC+3, no daylight saving).
- **EOD cut:** which session close or mark defines EOD for bonds, curves and FX is decision DEC04. Until it is settled:
  - bonds use the MOEX main-session EOD (preliminary) and the next-morning final;
  - FX uses the internal 19:00 mark when supplied, otherwise it is labelled as a candidate;
  - CBR FX uses the effective date known at the cut.
- **Calendars:** the hub's calendars, including Russia's transferred working Saturdays, reach core only through the versioned calendar in the static snapshot.

## 7 Monitoring and alerting

The hub has quality checks and gap tracking but **no alerting** today. Add one notification channel (email or the bank messenger) for:

| Source | Alert |
|---|---|
| Hub | Failed or missed job; dataset stale beyond policy; gap turned unfillable; snapshot not published at the cut |
| Backend | EOD or intraday run failed or incomplete; coverage below threshold; T+1 official not attached by a set time; PnL break above threshold (DEC12) |
| Server | Disk usage, backup failure, certificate expiry, process down |

Health endpoints: hub `/health/ready`, backend `/health/ready` (database, latest snapshot age, worker heartbeat).

## 8 Backups and recovery

| Item | Method | Target |
|---|---|---|
| `mdhub` and `desk` databases | Nightly `pg_dump -Fc` per database, plus WAL archiving if RPO < 24 h is required | A backup location inside the bank, **not on the same disk** |
| Hub raw captures and snapshot bundles | File sync with checksums | Same |
| Configuration and secrets | Config in git; secrets in the bank's approved secret store | — |
| Drill | Quarterly restore to a clean environment, then verify run replay from a snapshot | Evidence recorded |

Initial goals carried from program ADR07: RPO 24 h, RTO 2 h. Confirm with the owner.

## 9 Security

- Services bind to localhost; only the reverse proxy listens on the network.
- Authentication and roles per [05](05_FRONTEND_INTEGRATION.md) §6.
- Each service has its own database role: the hub cannot read `desk`, and the backend has read-only access to hub data through the API only.
- The backend authenticates to the hub with its own bearer token (hub `MDHUB_API_TOKENS`), kept in the secret store.
- No secrets in repositories, URLs or logs. The hub already redacts secrets in logs and URLs; keep that behaviour.
