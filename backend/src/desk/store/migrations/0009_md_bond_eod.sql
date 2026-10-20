-- Market history for the bond charts (R7): the daily bond quotes and the KBD of every eod_final market snapshot the
-- desk has seen, and the terms of the bonds from the static snapshots (desk.marketdata.history). A read model, not
-- a run input: the runs read the bundles. Rebuilt from the snapshot cache when lost; dates older than the cache come
-- from the hub without being cached (history_days). Not append-only: per business date the newest snapshot (latest
-- known_at) wins, and a correction replaces the date's rows.

-- One row per eod_final business date loaded: the snapshot its rows come from and the KBD (zcyc) parameters.
CREATE TABLE md_kbd (
    business_date date PRIMARY KEY,
    snapshot_id   text NOT NULL,
    known_at      timestamptz NOT NULL,
    trade_date    date,                -- the KBD publication's own date
    params        jsonb,               -- {B1, B2, B3, T1, G1..G9} as published (decimal strings); null: none published
    loaded_at     timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- One quote per bond and date: of its boards the one with a price (legal_close, close, waprice: the run's order),
-- then the most trades. MOEX analytics as published: the effective yield to yield_date (offer or maturity).
CREATE TABLE md_bond_eod (
    business_date    date NOT NULL REFERENCES md_kbd ON DELETE CASCADE,
    secid            text NOT NULL,
    board            text NOT NULL,
    clean_pct        double precision,
    price_basis      text,
    yield_pct        double precision,
    yield_date       date,
    duration_days    double precision,
    gspread_bp       double precision,
    zspread_bp       double precision,
    accrued_per_bond double precision,
    face_value       double precision,
    volume_bonds     double precision,
    value            double precision,  -- turnover in currency_id (SUR: RUB)
    currency_id      text,
    num_trades       integer,
    PRIMARY KEY (business_date, secid)
);
CREATE INDEX md_bond_eod_secid ON md_bond_eod (secid, business_date);

-- The bond terms the history needs (identity, currency, coupon type, maturity), from the newest static snapshot.
CREATE TABLE md_instrument (
    secid         text PRIMARY KEY,
    isin          text,
    name          text,
    short_name    text,
    bond_class    text,
    coupon_type   text,
    face_unit     text,
    maturity_date date,
    is_perpetual  boolean,
    business_date date NOT NULL,        -- of the static snapshot
    snapshot_id   text NOT NULL
);
CREATE INDEX md_instrument_isin ON md_instrument (isin);

-- Every snapshot id the loader has handled, so that a cycle with nothing new costs one query: loaded (its rows are
-- in), older (a newer snapshot of its date was loaded first), skipped (not an eod_final market or static snapshot),
-- error (unreadable: a cached bundle never changes, so it is not tried again).
CREATE TABLE md_snapshot_load (
    snapshot_id   text PRIMARY KEY,
    kind          text,
    cut_type      text,
    business_date date,
    status        text NOT NULL CHECK (status IN ('loaded', 'older', 'skipped', 'error')),
    n_rows        integer NOT NULL DEFAULT 0,
    note          text,
    loaded_at     timestamptz NOT NULL DEFAULT clock_timestamp()
);
