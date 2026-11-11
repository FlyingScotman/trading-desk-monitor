-- Superseded runs can be deleted (TDM#634): after the history's two passes, a corrected report or a new program
-- version, every date keeps the run that replaced the older ones, and the older ones are of no screen: the owner
-- does not look at the history of replaced calculations. ADR-006 keeps the runs immutable; this is the one
-- explicit way out, narrow on purpose:
--
--  * a run is deleted only inside a transaction that said so (SET LOCAL desk.prune_superseded = 'on', which
--    desk.store.prune_superseded does and nothing else), so a stray DELETE, an UPDATE or a TRUNCATE still fails;
--  * only a complete EOD run (cut eod_final / eod_preliminary) that a newer complete EOD run of the same business
--    date replaced (a final run replaces any, a preliminary one a preliminary): the date keeps its newest run, so
--    its default run is never the one deleted;
--  * the rows of the run go with it (the row tables' triggers allow a DELETE on the same two conditions); a
--    scenario job's base run cannot go (the foreign keys), and INTRADAY runs keep their own rule (0010).
--
-- Which runs and when (the age, the batches, the quiet cycle) is the application's: desk.store.prune_superseded.
-- supersedes_run_id stays as a record of what a run replaced; the run it names may be gone, so it is no foreign key
-- any more (a delete would otherwise have to change the replacing run, which is immutable).
ALTER TABLE run DROP CONSTRAINT run_supersedes_run_id_fkey;

CREATE FUNCTION desk_run_prunable(rid bigint) RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT coalesce(current_setting('desk.prune_superseded', true), '') = 'on' AND EXISTS (
        SELECT 1 FROM run r
        WHERE r.run_id = rid AND r.run_type = 'EOD' AND r.status = 'complete'
          AND r.cut_type IN ('eod_final', 'eod_preliminary')
          AND EXISTS (SELECT 1 FROM run n
                      WHERE n.business_date = r.business_date AND n.run_type = 'EOD' AND n.status = 'complete'
                        AND n.run_id > r.run_id AND n.cut_type IN ('eod_final', 'eod_preliminary')
                        AND (n.cut_type = 'eod_final' OR r.cut_type = 'eod_preliminary')))
$$;

CREATE OR REPLACE FUNCTION desk_run_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND OLD.run_type = 'INTRADAY' AND OLD.status <> 'loading' THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'DELETE' AND desk_run_prunable(OLD.run_id) THEN
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
    IF TG_OP = 'DELETE' AND desk_run_prunable(OLD.run_id) THEN
        RETURN OLD;
    END IF;
    IF s IS DISTINCT FROM 'loading' THEN
        RAISE EXCEPTION 'desk store: run % is %, its rows are frozen', COALESCE(NEW.run_id, OLD.run_id), s;
    END IF;
    RETURN COALESCE(NEW, OLD);
END $$;
