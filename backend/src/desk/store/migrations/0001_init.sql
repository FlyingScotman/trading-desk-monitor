-- Desk store v1 (slice S5; ADR-006, 04 §2-§3). PostgreSQL 14+.
-- Private inputs are append-only and bitemporal; runs are immutable once complete or failed.

CREATE TABLE schema_migration (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

-- Positions: one loaded file gives one position set per book and valid date. A correction is a
-- new set (revision + 1) for the same (book_id, valid_date); nothing is updated or deleted.
-- "Positions of book B for date D as known at T": the set with the latest valid_date <= D, and of
-- those the latest recorded_at <= T. recorded_at is the insert's clock time, not the transaction
-- start; T is still a horizon, not an exact replay (a load committing after T can carry an earlier
-- recorded_at): run.position_set_ids is the source of truth for a run's inputs. A closed or dropped
-- book is an empty set (no position_input rows).
CREATE TABLE position_set (
    set_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id            text NOT NULL,
    valid_date         date NOT NULL,
    revision           integer NOT NULL,
    recorded_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    recorded_by        text,
    source_name        text NOT NULL,
    source_file_sha256 text NOT NULL,
    UNIQUE (book_id, valid_date, revision)
);

CREATE TABLE position_input (
    set_id      bigint NOT NULL REFERENCES position_set,
    position_id text NOT NULL,
    secid       text NOT NULL,
    quantity    double precision NOT NULL,  -- number of bonds; negative is short
    PRIMARY KEY (set_id, position_id)
);

-- Runs (04 §3). run_type: EOD / INTRADAY / TEST / REPLAY. The default run of a date is the latest
-- complete EOD run on eod_final, otherwise on eod_preliminary.
CREATE TABLE run (
    run_id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_type              text NOT NULL CHECK (run_type IN ('EOD', 'INTRADAY', 'TEST', 'REPLAY')),
    business_date         date NOT NULL,
    prev_date             date,
    cut_type              text NOT NULL,
    status                text NOT NULL DEFAULT 'loading' CHECK (status IN ('loading', 'complete', 'failed')),
    started_at            timestamptz NOT NULL DEFAULT now(),
    completed_at          timestamptz,
    supersedes_run_id     bigint REFERENCES run,
    market_snapshot_id    text NOT NULL,
    static_snapshot_id    text NOT NULL,
    prev_market_snapshot_id text,
    private_inputs_hash   text NOT NULL,     -- sha256 over the position sets used
    position_set_ids      bigint[] NOT NULL,
    positions_known_at    timestamptz NOT NULL,
    core_version          text NOT NULL,
    backend_version       text NOT NULL,
    backend_git_sha       text,
    lineage               jsonb NOT NULL,    -- EodContext.lineage() and the rest of 04 §3
    coverage              jsonb,             -- {book: {quality: n}, "_total": {...}}
    totals                jsonb,             -- {pv_rub, dv01_rub, cs01_rub, pnl: {...}} per book and "_total"
    error                 text
);
CREATE INDEX run_by_date ON run (business_date, run_type, status);

CREATE TABLE run_log (
    run_id  bigint NOT NULL REFERENCES run,
    seq     integer NOT NULL,
    level   text NOT NULL CHECK (level IN ('INFO', 'WARN', 'ERROR')),
    message text NOT NULL,
    PRIMARY KEY (run_id, seq)
);

-- Per-position facts. "fact" is desk.publish.tdm_run.position_row (one format for both stores);
-- the typed columns are for filters and sums.
CREATE TABLE run_position (
    run_id      bigint NOT NULL REFERENCES run,
    position_id text NOT NULL,
    ord         integer NOT NULL,            -- the book's order, as published
    book_id     text NOT NULL,
    secid       text NOT NULL,
    quantity    double precision NOT NULL,
    quality     text,                        -- null: not valued (no data at all)
    reason      text,                        -- why not exact / not valued
    pv_rub      double precision,
    dv01_rub    double precision,
    cs01_rub    double precision,
    fact        jsonb,                       -- null when not valued
    pnl         jsonb,                       -- FlashPnl.as_tdm() plus "extra"; null without PnL
    no_pnl      jsonb,                       -- {reason, detail} when priced but without PnL
    PRIMARY KEY (run_id, position_id)
);

-- Run-level reference and market data as published: kind in books / issuers / curves / fx / indices.
CREATE TABLE run_reference (
    run_id bigint NOT NULL REFERENCES run,
    kind   text NOT NULL,
    data   jsonb NOT NULL,
    PRIMARY KEY (run_id, kind)
);

-- Core results (rub-portfolio-risk-result.v2), the exact bytes core wrote; sha256 verified on read.
CREATE TABLE run_core_result (
    run_id        bigint NOT NULL REFERENCES run,
    result_sha256 text NOT NULL,
    label         text NOT NULL,             -- spread kind or call label
    body          text NOT NULL,
    PRIMARY KEY (run_id, result_sha256)
);

-- Z-spreads the model-price rung used, per run and bond. The next run on a later date carries the
-- latest one, so a model price moves with the curve, not with the day's median (S4 open item).
CREATE TABLE model_spread (
    run_id        bigint NOT NULL REFERENCES run,
    secid         text NOT NULL,
    business_date date NOT NULL,
    z_spread_bp   double precision NOT NULL,
    source        text NOT NULL,
    PRIMARY KEY (run_id, secid)
);

-- Immutability: rows of a run that is no longer loading cannot change; input rows never change;
-- no store table but schema_migration can be truncated.
CREATE FUNCTION desk_forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'desk store: % on % is not allowed (append-only)', TG_OP, TG_TABLE_NAME;
END $$;

CREATE FUNCTION desk_run_rows_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s text;
BEGIN
    SELECT status INTO s FROM run WHERE run_id = COALESCE(NEW.run_id, OLD.run_id);
    IF s IS DISTINCT FROM 'loading' THEN
        RAISE EXCEPTION 'desk store: run % is %, its rows are frozen', COALESCE(NEW.run_id, OLD.run_id), s;
    END IF;
    RETURN COALESCE(NEW, OLD);
END $$;

CREATE FUNCTION desk_run_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.status <> 'loading' THEN
        RAISE EXCEPTION 'desk store: run % is %, it cannot change', OLD.run_id, OLD.status;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER position_set_append_only BEFORE UPDATE OR DELETE ON position_set
    FOR EACH ROW EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER position_input_append_only BEFORE UPDATE OR DELETE ON position_input
    FOR EACH ROW EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER run_frozen BEFORE UPDATE OR DELETE ON run
    FOR EACH ROW EXECUTE FUNCTION desk_run_frozen();
CREATE TRIGGER run_log_frozen BEFORE INSERT OR UPDATE OR DELETE ON run_log
    FOR EACH ROW EXECUTE FUNCTION desk_run_rows_frozen();
CREATE TRIGGER run_position_frozen BEFORE INSERT OR UPDATE OR DELETE ON run_position
    FOR EACH ROW EXECUTE FUNCTION desk_run_rows_frozen();
CREATE TRIGGER run_reference_frozen BEFORE INSERT OR UPDATE OR DELETE ON run_reference
    FOR EACH ROW EXECUTE FUNCTION desk_run_rows_frozen();
CREATE TRIGGER run_core_result_frozen BEFORE INSERT OR UPDATE OR DELETE ON run_core_result
    FOR EACH ROW EXECUTE FUNCTION desk_run_rows_frozen();
CREATE TRIGGER model_spread_frozen BEFORE INSERT OR UPDATE OR DELETE ON model_spread
    FOR EACH ROW EXECUTE FUNCTION desk_run_rows_frozen();
CREATE TRIGGER position_set_no_truncate BEFORE TRUNCATE ON position_set
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER position_input_no_truncate BEFORE TRUNCATE ON position_input
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER run_no_truncate BEFORE TRUNCATE ON run
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER run_log_no_truncate BEFORE TRUNCATE ON run_log
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER run_position_no_truncate BEFORE TRUNCATE ON run_position
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER run_reference_no_truncate BEFORE TRUNCATE ON run_reference
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER run_core_result_no_truncate BEFORE TRUNCATE ON run_core_result
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER model_spread_no_truncate BEFORE TRUNCATE ON model_spread
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
