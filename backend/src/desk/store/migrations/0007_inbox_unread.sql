-- Files the scan never reads: match says they are not the adapter's ('ignored'), or their report date is behind the
-- lookback ('ignored_too_old'). A folder of years of reports holds thousands of them; they are remembered by path,
-- size and modification time, never hashed, archived or ledgered (inbox_file_status keeps the files the scan read
-- or will read). Not append-only: a row is the latest word on that file version, rewritten when the adapter changes
-- (ignored) or the lookback grows (too old). Rows written before 0007 stay where they are.
CREATE TABLE inbox_unread (
    path                text NOT NULL,
    size_bytes          bigint NOT NULL,
    mtime               timestamptz NOT NULL,
    status              text NOT NULL CHECK (status IN ('ignored', 'ignored_too_old')),
    adapter_fingerprint text NOT NULL,
    source              text,
    report_date         date,
    at                  timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (path, size_bytes, mtime)
);
