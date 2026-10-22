-- The datasets of the private contract beyond positions (08 §10, private/1.2): ocp, cash_balances, pnl_official,
-- trade_terms, and any dataset that gets a schema file later. They are stored generically, so a new dataset needs a
-- schema file and no migration. Like position_set, a set is one accepted unit (dataset, scope unit, valid date) of
-- one inbox file and a revision of that unit (08 §6.6): the current one is the greatest by store.CURRENT_SET. Its
-- rows are the validated canonical rows. Every table here is append-only.
CREATE TABLE dataset_set (
    set_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset             text NOT NULL,
    scope               jsonb NOT NULL,          -- the scope unit, e.g. {"entity": "E1", "book_id": "B1"}
    scope_key           text NOT NULL,           -- its canonical text (sorted keys): what revisions are grouped by
    valid_date          date NOT NULL,           -- the unit's business date (a multi-date file: the row's, 08 §6.1)
    revision            integer NOT NULL,        -- 1, 2, ... per (dataset, scope_key, valid_date)
    source              text NOT NULL,
    priority            integer,                 -- null: a source without priority (08 §6.6)
    report_date         date NOT NULL,           -- the file's report key (08 §6.6), also for a multi-date file
    report_ts           timestamptz,
    seq                 integer,
    generated_at        timestamptz,
    file_mtime          timestamptz,
    inbox_file_sha256   text NOT NULL REFERENCES inbox_file,
    source_name         text NOT NULL,           -- the file name as delivered
    adapter_fingerprint text NOT NULL,
    unit_hash           text NOT NULL,           -- validate.unit_hash: canonical fields only
    row_count           integer NOT NULL,
    recorded_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (dataset, scope_key, valid_date, revision)
);
CREATE INDEX dataset_set_by_date ON dataset_set (dataset, valid_date);

-- The rows of a set: canonical fields as validated (amounts as decimal strings, dates as YYYY-MM-DD, codes upper
-- case), without _src and x_* (kept beside, excluded from the change hash, 08 §6.6).
CREATE TABLE dataset_row (
    set_id      bigint NOT NULL REFERENCES dataset_set,
    row_no      integer NOT NULL,                -- 1, 2, ... in the order of the dataset key
    row_key     text NOT NULL,                   -- the dataset key (08 §10) joined by '|', absent parts empty
    data        jsonb NOT NULL,
    src         text,                            -- the report row (_src), for drill-down
    x           jsonb,                           -- the report's x_* fields as they came
    PRIMARY KEY (set_id, row_no)
);

-- An inbox unit of a dataset other than positions points at its dataset set (set_id stays for positions).
ALTER TABLE inbox_unit ADD COLUMN dataset_set_id bigint REFERENCES dataset_set;

-- Reference tables of reference() beyond books (08 §7, §10: entities, funding_rules), bitemporal like book_rev: a
-- new revision only when a row's content changed. A row is keyed by (kind, ref_key, valid_from); in force on date D
-- as known at T: per ref_key the revision recorded <= T with the greatest effective date <= D (valid_from, else the
-- day it was recorded; the first revision of a key: always), then the latest.
CREATE TABLE reference_rev (
    rev_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind                text NOT NULL,           -- entities | funding_rules
    ref_key             text NOT NULL,           -- the row's key (spi.REFERENCE_KEYS) joined by '|'
    valid_from          date,
    active              boolean NOT NULL,
    content             jsonb NOT NULL,          -- the validated row without _src
    recorded_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    adapter_fingerprint text NOT NULL,
    content_sha256      text NOT NULL
);
CREATE INDEX reference_rev_by_key ON reference_rev (kind, ref_key, rev_id DESC);

-- Typed views of the rows, every revision with its set (the current ones: store.dataset_rows). A field added by a
-- later contract minor is in dataset_row.data at once; a view lists it only after its own migration.
CREATE VIEW ocp_row AS
SELECT s.set_id, s.valid_date, s.source, s.revision, r.row_no, r.data->>'entity' AS entity,
       r.data->>'book_id' AS book_id, r.data->>'ccy' AS ccy, (r.data->>'ocp_ccy')::numeric AS ocp_ccy,
       (r.data->>'ocp_rub')::numeric AS ocp_rub, (r.data->>'rate_rub_per_unit')::numeric AS rate_rub_per_unit,
       r.src, r.x
FROM dataset_row r JOIN dataset_set s USING (set_id) WHERE s.dataset = 'ocp';

CREATE VIEW cash_balance_row AS
SELECT s.set_id, s.valid_date, s.source, s.revision, r.row_no, r.data->>'entity' AS entity,
       r.data->>'book_id' AS book_id, r.data->>'ccy' AS ccy, r.data->>'account_id' AS account_id,
       (r.data->>'balance_ccy')::numeric AS balance_ccy, r.data->>'account_kind' AS account_kind,
       r.data->>'ledger' AS ledger, r.src, r.x
FROM dataset_row r JOIN dataset_set s USING (set_id) WHERE s.dataset = 'cash_balances';

CREATE VIEW pnl_official_row AS
SELECT s.set_id, s.valid_date, s.source, s.revision, r.row_no, r.data->>'entity' AS entity,
       r.data->>'book_id' AS book_id, r.data->>'line_id' AS line_id, r.data->>'position_id' AS position_id,
       r.data->>'trade_id' AS trade_id, r.data->>'instrument_id' AS instrument_id, r.data->>'isin' AS isin,
       r.data->>'ccy' AS ccy, (r.data->>'pnl_daily_rub')::numeric AS pnl_daily_rub,
       (r.data->>'pnl_pl_daily_rub')::numeric AS pnl_pl_daily_rub,
       (r.data->>'pnl_oci_daily_rub')::numeric AS pnl_oci_daily_rub,
       (r.data->>'pnl_mtd_rub')::numeric AS pnl_mtd_rub, (r.data->>'pnl_ytd_rub')::numeric AS pnl_ytd_rub,
       r.data, r.src, r.x
FROM dataset_row r JOIN dataset_set s USING (set_id) WHERE s.dataset = 'pnl_official';

CREATE VIEW trade_terms_row AS
SELECT s.set_id, s.valid_date, s.source, s.revision, r.row_no, r.data->>'trade_id' AS trade_id,
       r.data->>'leg_id' AS leg_id, r.data->>'book_id' AS book_id, r.data->>'product_kind' AS product_kind,
       (r.data->>'trade_date')::date AS trade_date, (r.data->>'value_date')::date AS value_date,
       r.data->>'base_ccy' AS base_ccy, r.data->>'quote_ccy' AS quote_ccy, r.data->>'side' AS side,
       (r.data->>'notional_base')::numeric AS notional_base, (r.data->>'notional_quote')::numeric AS notional_quote,
       (r.data->>'contract_rate')::numeric AS contract_rate, coalesce((r.data->>'ndf')::boolean, false) AS ndf,
       r.data->>'settle_ccy' AS settle_ccy, (r.data->>'fixing_date')::date AS fixing_date,
       r.data->>'fixing_source' AS fixing_source, r.data->>'counterparty_id' AS counterparty_id,
       (r.data->>'pv_rub')::numeric AS pv_rub, r.data, r.src, r.x
FROM dataset_row r JOIN dataset_set s USING (set_id) WHERE s.dataset = 'trade_terms';

CREATE TRIGGER dataset_set_append_only BEFORE UPDATE OR DELETE ON dataset_set
    FOR EACH ROW EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER dataset_row_append_only BEFORE UPDATE OR DELETE ON dataset_row
    FOR EACH ROW EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER reference_rev_append_only BEFORE UPDATE OR DELETE ON reference_rev
    FOR EACH ROW EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER dataset_set_no_truncate BEFORE TRUNCATE ON dataset_set
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER dataset_row_no_truncate BEFORE TRUNCATE ON dataset_row
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER reference_rev_no_truncate BEFORE TRUNCATE ON reference_rev
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
