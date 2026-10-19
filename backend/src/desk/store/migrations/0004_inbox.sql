-- The inbox (slice S7a; 08 §6, §13): a ledger of bank report files read through the private adapter, the
-- position sets they give, and bitemporal books from reference(). Every table here is append-only: a status
-- change is a new inbox_file_status row, a book change a new book_rev row; nothing is updated or deleted.

-- One row per distinct content, keyed by SHA-256: where and how it was first seen. What the adapter made of it
-- (source, report date, ...) depends on the adapter's version, so it is on each attempt (inbox_file_status).
CREATE TABLE inbox_file (
    sha256      text PRIMARY KEY,
    path        text NOT NULL,                 -- the first path seen
    name        text NOT NULL,
    size_bytes  bigint NOT NULL,
    mtime       timestamptz NOT NULL,          -- the file's modification time (last part of the revision key)
    first_seen  timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Every path, size and modification time a scan took in, so an unchanged file is not hashed again. A copy of
-- known content is a duplicate (same source and report date: nothing loaded) or a stale_copy (another report
-- date: yesterday's file under today's name, 08 §13); new content is 'new'.
CREATE TABLE inbox_sighting (
    path        text NOT NULL,
    size_bytes  bigint NOT NULL,
    mtime       timestamptz NOT NULL,
    sha256      text NOT NULL REFERENCES inbox_file,
    kind        text NOT NULL CHECK (kind IN ('new', 'duplicate', 'stale_copy')),
    source      text,                          -- what match said of this path (copies only)
    report_date date,
    valid_date  date,
    at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (path, size_bytes, mtime)
);

-- The status history of a file: one row per status change (08 §13), the latest is current. The hit of match
-- and the business date are those of this attempt. Errors carry the exception type and its frames, never the
-- message (08 §6.8).
CREATE TABLE inbox_file_status (
    status_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sha256              text NOT NULL REFERENCES inbox_file,
    at                  timestamptz NOT NULL DEFAULT clock_timestamp(),
    status              text NOT NULL CHECK (status IN ('waiting', 'waiting_calendar', 'ignored', 'match_failed',
                            'accepted', 'partially_accepted', 'rejected', 'failed', 'timed_out', 'duplicate',
                            'stale_copy', 'superseded')),
    adapter_fingerprint text NOT NULL,
    path                text NOT NULL,          -- the path read: the inbox folder, or the archive copy
    archive_path        text,                   -- null: not matched, not archived
    source              text,
    report_date         date,
    report_ts           timestamptz,
    seq                 integer,
    generated_at        timestamptz,
    date_from           text,
    valid_date          date,
    error_code          text,
    error_type          text,
    error_frames        jsonb
);
CREATE INDEX inbox_file_status_by_file ON inbox_file_status (sha256, status_id DESC);

-- One row per (dataset, valid date, scope unit) an attempt touched. set_id: the position set the unit stored,
-- or the current one it equals (no new revision); null for a rejected or superseded unit.
CREATE TABLE inbox_unit (
    unit_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status_id   bigint NOT NULL REFERENCES inbox_file_status,
    sha256      text NOT NULL REFERENCES inbox_file,
    dataset     text NOT NULL,
    valid_date  date,
    report_date date NOT NULL,
    scope       jsonb NOT NULL,
    status      text NOT NULL CHECK (status IN ('accepted', 'rejected', 'superseded')),
    unit_hash   text,
    row_count   integer NOT NULL,
    set_id      bigint,                        -- REFERENCES position_set (added below)
    at          timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE inbox_issue (
    issue_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status_id   bigint NOT NULL REFERENCES inbox_file_status,
    sha256      text NOT NULL REFERENCES inbox_file,
    unit_id     bigint REFERENCES inbox_unit,
    severity    text NOT NULL CHECK (severity IN ('error', 'warning', 'info')),
    code        text NOT NULL,
    src         text,
    scope       jsonb,
    message     text,
    count       integer NOT NULL DEFAULT 1,
    origin      text NOT NULL CHECK (origin IN ('adapter', 'backend'))
);

-- Every status change writes an event; the orchestrator starts dependent runs from them (ROADMAP §1).
CREATE TABLE event (
    event_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    type        text NOT NULL,
    source      text,
    dataset     text,
    valid_date  date,
    sha256      text,
    counts      jsonb
);
CREATE INDEX event_by_type ON event (type, event_id);

-- Books from reference() (08 §7, §10 books), bitemporal: a new revision only when a book's content changed.
-- In force on date D as known at T: the latest revision recorded <= T whose valid_from (else the day it was
-- recorded; the first revision of a book: always) is <= D.
CREATE TABLE book_rev (
    rev_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id             text NOT NULL,
    name                text NOT NULL,
    desk                text NOT NULL,
    book_type           text NOT NULL,
    entity              text NOT NULL,
    ocp_to              text,
    trader              text,
    active              boolean NOT NULL,
    valid_from          date,
    recorded_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    adapter_fingerprint text NOT NULL,
    content_sha256      text NOT NULL
);
CREATE INDEX book_rev_by_book ON book_rev (book_id, rev_id DESC);

-- Position sets from the inbox: one per accepted positions unit (book, valid date). Legacy sets of
-- desk load-positions are source 'load-positions', priority 0 (08 §6.6) and have no report key. Within a
-- (book, valid date) the current set is the greatest (priority, report_date, report_ts, seq, generated_at,
-- file_mtime), a missing part below a present one; legacy sets by recorded_at as before.
ALTER TABLE position_set
    ADD COLUMN source              text NOT NULL DEFAULT 'load-positions',
    ADD COLUMN priority            integer DEFAULT 0,  -- null: an inbox source without priority
    ADD COLUMN report_date         date,
    ADD COLUMN report_ts           timestamptz,
    ADD COLUMN seq                 integer,
    ADD COLUMN generated_at        timestamptz,
    ADD COLUMN file_mtime          timestamptz,
    ADD COLUMN inbox_file_sha256   text REFERENCES inbox_file,
    ADD COLUMN adapter_fingerprint text,
    ADD COLUMN unit_hash           text;
CREATE INDEX position_set_by_book_date ON position_set (book_id, valid_date);

-- The private contract v1 fields of a position (08 §10 positions); secid is instrument_id, quantity the
-- number of bonds when the report gives it. Amounts as numeric, exactly as reported.
ALTER TABLE position_input
    ADD COLUMN asset_class            text,
    ADD COLUMN ccy                    text,
    ADD COLUMN face_amount_ccy        numeric,
    ADD COLUMN face_outstanding_ccy   numeric,
    ADD COLUMN amount_ccy             numeric,
    ADD COLUMN value_date             date,
    ADD COLUMN deal_id                text,
    ADD COLUMN source_instrument_id   text,
    ADD COLUMN source_instrument_name text,
    ADD COLUMN isin                   text,
    ADD COLUMN regnum                 text,
    ADD COLUMN moex_secid             text,
    ADD COLUMN mark_price_pct         numeric,
    ADD COLUMN accrued_ccy            numeric,
    ADD COLUMN mv_rub                 numeric,
    ADD COLUMN book_value_rub         numeric,
    ADD COLUMN purchase_date          date,
    ADD COLUMN purchase_price_pct     numeric,
    ADD COLUMN purchase_yield_pct     numeric,
    ADD COLUMN contract_rate_pct      numeric,
    ADD COLUMN src                    text,    -- the report row (_src), for drill-down
    ADD COLUMN x                      jsonb;   -- the report's x_* fields as they came

ALTER TABLE inbox_unit ADD FOREIGN KEY (set_id) REFERENCES position_set;

CREATE TRIGGER inbox_file_append_only BEFORE UPDATE OR DELETE ON inbox_file
    FOR EACH ROW EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER inbox_sighting_append_only BEFORE UPDATE OR DELETE ON inbox_sighting
    FOR EACH ROW EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER inbox_file_status_append_only BEFORE UPDATE OR DELETE ON inbox_file_status
    FOR EACH ROW EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER inbox_unit_append_only BEFORE UPDATE OR DELETE ON inbox_unit
    FOR EACH ROW EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER inbox_issue_append_only BEFORE UPDATE OR DELETE ON inbox_issue
    FOR EACH ROW EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER event_append_only BEFORE UPDATE OR DELETE ON event
    FOR EACH ROW EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER book_rev_append_only BEFORE UPDATE OR DELETE ON book_rev
    FOR EACH ROW EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER inbox_file_no_truncate BEFORE TRUNCATE ON inbox_file
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER inbox_sighting_no_truncate BEFORE TRUNCATE ON inbox_sighting
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER inbox_file_status_no_truncate BEFORE TRUNCATE ON inbox_file_status
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER inbox_unit_no_truncate BEFORE TRUNCATE ON inbox_unit
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER inbox_issue_no_truncate BEFORE TRUNCATE ON inbox_issue
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER event_no_truncate BEFORE TRUNCATE ON event
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER book_rev_no_truncate BEFORE TRUNCATE ON book_rev
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
