-- Read models of the concept pages (R23; desk.api.concepts, desk.marketdata.static_facts). None of them is a run
-- input: each is rebuilt from the runs or from the snapshots when lost, so they are not append-only.

-- One row per complete run: its sums (risk by curve, CS01, OFZ basis, DV01 by book, MV by type and currency, PnL by
-- factor) and per position the numbers the series need (face, DV01, CS01, basis, MV, risk by curve, PnL, price,
-- spread). A run never changes, so a row is written once (desk.api.concepts.ensure_summaries: at publication, and
-- for a run of an older version on first read); ``version`` is the summary's own format.
CREATE TABLE run_summary (
    run_id      bigint PRIMARY KEY REFERENCES run ON DELETE CASCADE,
    version     integer NOT NULL,
    data        jsonb NOT NULL,
    built_at    timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- The static snapshots' facts the pages show beside the runs: per bond its display and size facts (hub static 1.1:
-- issuer short name, group, sector, Latin name, issue size, coupon frequency) and the market's payment calendar
-- (coupons, amortizations, redemptions, offers of every active bond) aggregated by day, type and kind, with the
-- flows themselves for the drill-down. The newest few static snapshots are kept (desk.marketdata.static_facts.KEEP).
CREATE TABLE md_static_summary (
    snapshot_id   text PRIMARY KEY,
    business_date date NOT NULL,
    version       integer NOT NULL,
    data          jsonb NOT NULL,          -- {"calendar": {...}, "coverage": {...}}
    loaded_at     timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX md_static_summary_date ON md_static_summary (business_date);

CREATE TABLE md_static_bond (
    snapshot_id   text NOT NULL REFERENCES md_static_summary ON DELETE CASCADE,
    secid         text NOT NULL,
    data          jsonb NOT NULL,          -- desk.marketdata.static_facts.bond_facts
    PRIMARY KEY (snapshot_id, secid)
);

CREATE TABLE md_market_flow (
    snapshot_id   text NOT NULL REFERENCES md_static_summary ON DELETE CASCADE,
    pay_date      date NOT NULL,
    secid         text NOT NULL,
    kind          text NOT NULL,           -- cpn, amort, mat, put, call
    market        text NOT NULL,           -- rub, fx
    type          text NOT NULL,           -- PD, PK, IN, CORP_FIX, CORP_FLT, SUB, OTHER (rub); the face currency (fx)
    ccy           text NOT NULL,
    amount        double precision,        -- in ccy for the whole issue; null: unknown
    per_bond      double precision,
    est           boolean NOT NULL DEFAULT false
);
CREATE INDEX md_market_flow_date ON md_market_flow (snapshot_id, pay_date);

-- CBR key-rate meetings as the newest market snapshot publishes them (hub dataset cbr_meetings): past decisions and
-- the announced (possibly estimated) dates ahead.
CREATE TABLE md_cbr_meeting (
    meeting_date      date PRIMARY KEY,
    effective_date    date,
    is_estimated      boolean NOT NULL,
    is_extraordinary  boolean NOT NULL,
    decision          text,
    decision_rate_pct double precision,
    prev_rate_pct     double precision,
    snapshot_id       text NOT NULL,
    business_date     date NOT NULL
);
