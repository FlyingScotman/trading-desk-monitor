-- Intraday runs (R11): the book of the day valued on the hub's intraday cuts (run_type INTRADAY, cut_type
-- intraday) every half hour, and on eod_preliminary in the evening (an EOD run, as before). A run records the
-- nominal instant of its market cut and the cut of the snapshot its flash PnL is measured against (the previous
-- business day's eod_final, else an earlier final or a preliminary close: lineage->'pnl_basis' says which).
ALTER TABLE run ADD COLUMN cut_ts timestamptz;      -- the market snapshot's cut_ts; null for runs made before R11
ALTER TABLE run ADD COLUMN prev_cut_type text;      -- cut of prev_market_snapshot_id; null without one (or before R11)

-- Only the latest INTRADAY run of a date is kept (desk.store.prune_intraday): an older one, complete or failed, can
-- be deleted with its rows. Every other run stays immutable once it is no longer loading.
CREATE OR REPLACE FUNCTION desk_run_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND OLD.run_type = 'INTRADAY' AND OLD.status <> 'loading' THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'DELETE' OR OLD.status <> 'loading' THEN
        RAISE EXCEPTION 'desk store: run % is %, it cannot change', OLD.run_id, OLD.status;
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION desk_run_rows_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s text; t text;
BEGIN
    SELECT status, run_type INTO s, t FROM run WHERE run_id = COALESCE(NEW.run_id, OLD.run_id);
    IF TG_OP = 'DELETE' AND t = 'INTRADAY' AND s <> 'loading' THEN
        RETURN OLD;
    END IF;
    IF s IS DISTINCT FROM 'loading' THEN
        RAISE EXCEPTION 'desk store: run % is %, its rows are frozen', COALESCE(NEW.run_id, OLD.run_id), s;
    END IF;
    RETURN COALESCE(NEW, OLD);
END $$;

CREATE INDEX run_intraday ON run (business_date, run_id) WHERE run_type = 'INTRADAY';
