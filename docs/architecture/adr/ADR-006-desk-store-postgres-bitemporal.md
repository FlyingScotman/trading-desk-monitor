# ADR-006 Desk store on PostgreSQL with append-only, bitemporal private data

Status: proposed (16 October 2026).

## Context

The `tdm` prototype uses SQLite with the standard library only, which suited a locked-down Windows PC. Its data model (contract 1.3) is thorough, but it has gaps:
- private inputs are current-version upserts with no knowledge time;
- official PnL and scenario results mutate published runs in place;
- assembly reads current global definitions, so old runs are not reproducible.

The desk now runs on a Windows server inside the network, where PostgreSQL is also used by the inside part of the hub.

## Decision

1. The desk store is a PostgreSQL database `desk` on the inside Windows server. The hub's `mdhub` database lives on the VPS ([ADR-001](ADR-001-hosting-topology.md)); the two are never joined. The backend reads market data only through snapshots ([ADR-002](ADR-002-materialized-market-snapshots.md)).
2. **Private data is bitemporal and append-only.** Positions, trades, OTC terms, funding rules, cash balances, trader marks, the internal FX mark and official PnL each carry:
   - `valid_date` (business date the record describes);
   - `recorded_at` (when the desk learned it);
   - `source_file_sha256`;
   - `revision`.

   A correction is a new revision. Nothing is overwritten.
3. **Runs are immutable.** A run records its full lineage ([04](../04_BACKEND.md) §3). Official PnL and scenario results are attachments with their own revisions, keyed to the run.
   Two kinds of run are deleted, each by its own rule in the store's triggers (migrations 0010 and 0024), and nothing else is:
   - the older **INTRADAY** runs of a date (`store.prune_intraday`): a replaced run stays `store.INTRADAY_GRACE` (an hour) after the run that replaced it, so a page opened on it keeps reading it (TDM#675);
   - a **superseded** EOD run (TDM#634): a complete run of cut `eod_final` / `eod_preliminary` that a newer complete EOD run of the same business date replaced (a final run replaces any, a preliminary one a preliminary one). The owner does not look at the history of replaced calculations, and the two passes of the history ([04](../04_BACKEND.md), TDM#627) and every correction or new program version leave one per date (about 7 MB with 2000 positions, 1.5-2 GB a year). The delete is one explicit store operation, `store.prune_superseded`, which says so in its transaction (`SET LOCAL desk.prune_superseded = 'on'`); without the word, or for any other run, the triggers refuse as before, so a stray `DELETE`, an `UPDATE` or a `TRUNCATE` still fails. The date's newest run is never deleted, nor a run a scenario job was made on (foreign keys); a run is deleted only after its replacement is `prune_superseded_days` old (default 14, not less than the 14 daily backups, so every backup kept holds the run when it is deleted), in batches of a few runs per transaction (a connection in autocommit, outside a transaction: `prune_superseded` refuses any other), by the autopilot's prune step in a cycle that made no run (setting `prune_superseded`, on by default). The space goes back to the tables by the server's autovacuum (on in the embedded server); a server without it gets `VACUUM` of the run tables after a large prune, once a day at most. The size is estimated once per call, before the first batch, from the planner's row counts (no scan of the run tables inside the delete's transaction). The day page marks a run whose replaced run is gone (`supersedes_pruned`). `run.supersedes_run_id` stays as a record of what a run replaced and is no foreign key any more: the run it names may be gone. The files give the space back to the disk only with `VACUUM FULL` (`store.compact_runs`, `desk prune --compact`), which stops reads of the runs while it rewrites them, so it is the owner's choice.
4. The `tdm` SQLite schema and contract 1.3 are the **data-model prototype**. Migrate their content and semantics; do not port the upsert behaviour.

## Consequences

- Replay of any past run, T+1 restatements and break investigations become possible (DEC11).
- The backend gains a Postgres dependency (psycopg) and migrations.
- The stdlib-only constraint is dropped. The Windows Python 3.11 environment is pinned and installed offline from a wheelhouse ([06](../06_DEPLOYMENT_AND_OPERATIONS.md) §7).
