-- Day status (S7b, ROADMAP §0.2 B5): runs write events too, so /api/v1/events is one feed of what happened (inputs
-- and runs) with one cursor, event_id, that the SSE stream (05 §5) resumes from (Last-Event-ID). run.published when
-- a run completes, in the same transaction; run.failed when it fails or is reaped. A run's event keeps its type, cut
-- and the run it superseded in detail: an INTRADAY run can be pruned later (desk.store.prune_intraday), its event
-- stays. Earlier runs get no events: the day status reads the runs themselves, the feed starts here.
ALTER TABLE event ADD COLUMN run_id bigint;   -- no foreign key: a pruned intraday run leaves its event
ALTER TABLE event ADD COLUMN detail jsonb;
CREATE INDEX event_by_date ON event (valid_date, event_id);
