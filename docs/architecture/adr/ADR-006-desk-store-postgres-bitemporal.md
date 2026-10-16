# ADR-006 Desk store on PostgreSQL with append-only, bitemporal private data

Status: proposed (16 October 2026).

## Context

The `tdm` prototype uses SQLite with the standard library only, which suited a locked-down Windows PC. Its data model (contract 1.3) is thorough, but it has gaps:
- private inputs are current-version upserts with no knowledge time;
- official PnL and scenario results mutate published runs in place;
- assembly reads current global definitions, so old runs are not reproducible.

The system now runs on a server that already hosts PostgreSQL for the hub.

## Decision

1. The desk store is a separate PostgreSQL database `desk` on the same instance as `mdhub`. The two databases are never joined directly. The backend reads market data only through snapshots ([ADR-002](ADR-002-materialized-market-snapshots.md)).
2. **Private data is bitemporal and append-only.** Positions, trades, OTC terms, funding rules, cash balances, trader marks, the internal FX mark and official PnL each carry:
   - `valid_date` (business date the record describes);
   - `recorded_at` (when the desk learned it);
   - `source_file_sha256`;
   - `revision`.

   A correction is a new revision. Nothing is overwritten.
3. **Runs are immutable.** A run records its full lineage ([04](../04_BACKEND.md) §3). Official PnL and scenario results are attachments with their own revisions, keyed to the run.
4. The `tdm` SQLite schema and contract 1.3 are the **data-model prototype**. Migrate their content and semantics; do not port the upsert behaviour.

## Consequences

- Replay of any past run, T+1 restatements and break investigations become possible (DEC11).
- The backend gains a Postgres dependency (psycopg) and migrations.
- The stdlib-only constraint is dropped; the server Python environment is pinned and installed offline ([06](../06_DEPLOYMENT_AND_OPERATIONS.md)).
