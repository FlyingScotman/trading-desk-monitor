-- The inbox lookback (inbox_lookback_days): a report file whose report date is older than the lookback is
-- ledgered once as 'ignored_too_old' (terminal, not an error) and never read; a file waiting for a calendar
-- gets the same status once its report date falls behind the lookback, so it stops driving hub syncs.
ALTER TABLE inbox_file_status DROP CONSTRAINT inbox_file_status_status_check;
ALTER TABLE inbox_file_status ADD CONSTRAINT inbox_file_status_status_check
    CHECK (status IN ('waiting', 'waiting_calendar', 'ignored', 'ignored_too_old', 'match_failed', 'accepted',
                      'partially_accepted', 'rejected', 'failed', 'timed_out', 'duplicate', 'stale_copy',
                      'superseded'));
