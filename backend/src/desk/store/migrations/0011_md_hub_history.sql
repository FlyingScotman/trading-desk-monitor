-- Market history from the hub's history API (hub 0.2.0, /v1/history/*; desk.marketdata.hubhistory): bond EOD, KBD,
-- curve quote sets, fixings and FX rates of past dates in a few columnar calls instead of one snapshot per date.
-- A read model like 0009: nothing here is a run input (the runs read the bundles). The snapshot tables of 0009 stay
-- the fallback (a hub before 0.2.0, a token without scope read): the views md_bond_day and md_kbd_day give a day's
-- row of the history API when it has one, else the snapshot's.

-- Contract 1.2 analytics of the snapshots' quotes too (MOEX yields fields: linkers' ICPI, IR, BEI, CBR verbatim).
ALTER TABLE md_bond_eod ADD COLUMN icpi double precision, ADD COLUMN ir double precision,
    ADD COLUMN bei double precision, ADD COLUMN cbr double precision;

-- Every fetch of a history query writes its rows with a new fetch id; when the query's last page is in, the rows
-- of its range (and selection) from older fetches are deleted: rows the hub no longer has go, rows a later fetch
-- (another query over the same days) wrote stay.
CREATE SEQUENCE md_hub_fetch_seq;

-- /v1/history/bonds (board=primary): one row per trade date and bond. Prices as MOEX publishes them, clean_pct the
-- runs' price basis order (legal_close, close, waprice), yield_pct MOEX's effective yield to yield_date.
CREATE TABLE md_hub_bond (
    trade_date            date NOT NULL,
    secid                 text NOT NULL,
    board                 text NOT NULL,
    clean_pct             double precision,
    price_basis           text,
    close_pct             double precision,
    waprice_pct           double precision,
    yield_pct             double precision,
    yield_close_pct       double precision,
    yield_to_offer_pct    double precision,
    yield_date            date,
    yield_date_type       text,
    duration_days         double precision,
    gspread_bp            double precision,
    zspread_bp            double precision,
    accrued_per_bond      double precision,
    face_value            double precision,
    face_unit             text,
    currency_id           text,
    volume_bonds          double precision,
    value                 double precision,   -- turnover in currency_id (SUR: RUB)
    num_trades            integer,
    icpi                  double precision,
    ir                    double precision,
    bei                   double precision,
    cbr                   double precision,
    fetch_id              bigint NOT NULL,
    PRIMARY KEY (trade_date, secid)
);
CREATE INDEX md_hub_bond_secid ON md_hub_bond (secid, trade_date);

-- /v1/history/zcyc?rule=eod: the KBD publication of each day (parameters as decimal strings, as md_kbd.params).
CREATE TABLE md_hub_zcyc (
    trade_date      date PRIMARY KEY,
    trade_time      text,
    is_eod          boolean,
    params          jsonb NOT NULL,       -- {B1, B2, B3, T1, G1..G9}
    standard_yields jsonb,                -- [{tenor_years, yield_pct}] as published
    fetch_id        bigint NOT NULL
);

-- /v1/history/curves/{dataset}?rule=eod: the end-of-day quote set of each (day, source), one row per tenor.
CREATE TABLE md_hub_curve (
    dataset       text NOT NULL,
    snapshot_date date NOT NULL,
    source        text NOT NULL,
    tenor         text NOT NULL,
    quote_set_id  bigint,
    position      integer,
    tenor_months  integer,
    tenor_days    integer,
    bid_pct       numeric,
    mid_pct       numeric,
    ask_pct       numeric,
    quote_basis   text,
    fetched_at    timestamptz,
    labels        jsonb,
    fetch_id      bigint NOT NULL,
    PRIMARY KEY (dataset, snapshot_date, source, tenor)
);

-- /v1/history/fixings: md.fixing and the served RUSFAR series (unit pct = % p.a., index = a level).
CREATE TABLE md_hub_fixing (
    index_code      text NOT NULL,
    source          text NOT NULL,
    fixing_date     date NOT NULL,
    value           numeric,
    unit            text,
    effective_date  date,
    published_on    date,
    hub_ingested_at timestamptz,
    labels          jsonb,
    fetch_id        bigint NOT NULL,
    PRIMARY KEY (index_code, source, fixing_date)
);

-- /v1/history/fx?kind=cbr_official,moex_eod: RUB per unit (cbr_official every calendar day in force; moex_eod per
-- trade date and instrument, close and waprice per unit when MOEX quotes per a power of ten units).
CREATE TABLE md_hub_fx (
    kind       text NOT NULL,
    pair       text NOT NULL,
    date       date NOT NULL,
    secid      text NOT NULL DEFAULT '',
    board      text NOT NULL DEFAULT '',
    rate       numeric,
    close      numeric,
    waprice    numeric,
    per_unit   numeric,                   -- cbr_official: rate; moex_eod: close_per_unit, else waprice_per_unit
    tenor      text,
    nominal    integer,
    fetch_id   bigint NOT NULL,
    PRIMARY KEY (kind, pair, date, secid, board)
);

-- One row per history query the desk keeps: the tail (the open months, fetched again when the hub changed them)
-- and one chunk per closed month (bonds) or year (the rest), revalidated with the ETag of its first page. A fetch
-- that did not finish in a cycle keeps its cursor and goes on in the next one.
CREATE TABLE md_hub_job (
    dataset      text NOT NULL,         -- bonds, zcyc, curves/<dataset>, fixings, fx, held
    key          text NOT NULL,         -- tail, a month YYYY-MM, a year YYYY, held:<n>
    params       jsonb NOT NULL,        -- the query (from, to, ...; limit), without cursor
    period_from  date NOT NULL,
    period_to    date NOT NULL,
    etag         text,                  -- of the first page of the last complete fetch
    data_version text,
    cursor       text,                  -- a fetch in progress: the next page
    pending_etag text,                  -- its first page's ETag, until it completes
    fetch_id     bigint,
    rows         integer NOT NULL DEFAULT 0,
    pages        integer NOT NULL DEFAULT 0,
    wire_bytes   bigint NOT NULL DEFAULT 0,
    status       text NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'partial', 'done', 'error')),
    error        text,
    fetched_at   timestamptz,           -- last complete fetch
    checked_at   timestamptz,           -- last revalidation (a 304 or a fetch)
    PRIMARY KEY (dataset, key)
);

-- The bonds whose whole history was fetched first (the desk's positions), so a new position gets its history in
-- the next cycle rather than when the backfill of the whole market reaches its months.
CREATE TABLE md_hub_held (
    secid       text PRIMARY KEY,
    from_date   date NOT NULL,
    fetched_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- The API's state as last seen (one row): available, or why not (no_api: hub < 0.2.0; no_read: token scope).
CREATE TABLE md_hub_state (
    id          boolean PRIMARY KEY DEFAULT true CHECK (id),
    code        text NOT NULL,
    reason      text,
    hub_version text,
    index       jsonb,
    checked_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- A day of a bond: the history API's row, else the snapshot's (md_bond_eod).
CREATE VIEW md_bond_day AS
SELECT h.trade_date AS business_date, h.secid, h.board, h.clean_pct, h.price_basis, h.yield_pct, h.yield_date,
       h.duration_days, h.gspread_bp, h.zspread_bp, h.accrued_per_bond, h.face_value, h.volume_bonds, h.value,
       h.currency_id, h.num_trades, h.icpi, h.ir, h.bei, h.cbr, 'hub'::text AS origin
  FROM md_hub_bond h
UNION ALL
SELECT b.business_date, b.secid, b.board, b.clean_pct, b.price_basis, b.yield_pct, b.yield_date, b.duration_days,
       b.gspread_bp, b.zspread_bp, b.accrued_per_bond, b.face_value, b.volume_bonds, b.value, b.currency_id,
       b.num_trades, b.icpi, b.ir, b.bei, b.cbr, 'snapshot'::text
  FROM md_bond_eod b
 WHERE NOT EXISTS (SELECT 1 FROM md_hub_bond h WHERE h.trade_date = b.business_date AND h.secid = b.secid);

-- A day's KBD: the history API's publication, else the snapshot's.
CREATE VIEW md_kbd_day AS
SELECT z.trade_date AS business_date, z.trade_date, z.params, 'hub'::text AS origin FROM md_hub_zcyc z
UNION ALL
SELECT k.business_date, k.trade_date, k.params, 'snapshot'::text FROM md_kbd k
 WHERE NOT EXISTS (SELECT 1 FROM md_hub_zcyc z WHERE z.trade_date = k.business_date);
