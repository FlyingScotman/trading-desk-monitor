-- Manually entered CDS curves (TDM#652): the owner types the par spreads of a reference name by tenor, the hub has no
-- CDS quotes and no paid source is connected. A curve is quoted as the par coupon spread, in basis points, at the
-- tenors 3M 6M 1Y 2Y 3Y 5Y 7Y 10Y (the owner's decision of 19.11.2026; an assumption that can be changed, see
-- desk.marketdata.manual_cds), with a recovery rate (default 25 %, below 100 %) and the quote currency (default USD,
-- the developer's assumption). changed_at is the time of the INSERT, not of the commit.
--
-- Append-only, like instrument_alias_rev (0014): each change is a revision with who and when. A revision holds the
-- whole curve of one reference name from valid_from on; the curve in force on a date is the name's latest revision of
-- the latest valid_from <= that date. A revision with cleared = true withdraws the name from its valid_from on (until a
-- later curve is entered). name_key is the identity of the name (desk.marketdata.manual_cds.name_key: Unicode NFKC,
-- case folded, whitespace collapsed); name is how it was written. changed_by is 'local' (a loopback client, sign-in
-- off) or the user's name.
CREATE TABLE manual_cds_rev (
    rev_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name_key   text NOT NULL CHECK (name_key = btrim(name_key) AND char_length(name_key) BETWEEN 1 AND 120),
    name       text NOT NULL CHECK (name = btrim(name) AND char_length(name) BETWEEN 1 AND 120),
    valid_from date NOT NULL CHECK (valid_from BETWEEN DATE '2000-01-01' AND DATE '2100-12-31'),
    cleared    boolean NOT NULL DEFAULT false,
    spread_3m  numeric(10,4) CHECK (spread_3m  BETWEEN 0 AND 10000),
    spread_6m  numeric(10,4) CHECK (spread_6m  BETWEEN 0 AND 10000),
    spread_1y  numeric(10,4) CHECK (spread_1y  BETWEEN 0 AND 10000),
    spread_2y  numeric(10,4) CHECK (spread_2y  BETWEEN 0 AND 10000),
    spread_3y  numeric(10,4) CHECK (spread_3y  BETWEEN 0 AND 10000),
    spread_5y  numeric(10,4) CHECK (spread_5y  BETWEEN 0 AND 10000),
    spread_7y  numeric(10,4) CHECK (spread_7y  BETWEEN 0 AND 10000),
    spread_10y numeric(10,4) CHECK (spread_10y BETWEEN 0 AND 10000),
    recovery   numeric(5,4) CHECK (recovery >= 0 AND recovery < 1),
    ccy        text CHECK (ccy ~ '^[A-Z]{3}$'),
    changed_by text NOT NULL DEFAULT 'local',
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- a curve has at least one point, a recovery rate and a currency; a withdrawal has none of them
    CONSTRAINT manual_cds_rev_shape CHECK (
        CASE WHEN cleared
             THEN num_nonnulls(spread_3m, spread_6m, spread_1y, spread_2y, spread_3y, spread_5y, spread_7y,
                               spread_10y, recovery, ccy) = 0
             ELSE num_nonnulls(spread_3m, spread_6m, spread_1y, spread_2y, spread_3y, spread_5y, spread_7y,
                               spread_10y) >= 1 AND recovery IS NOT NULL AND ccy IS NOT NULL END)
);
CREATE INDEX manual_cds_rev_by_name ON manual_cds_rev (name_key, valid_from DESC, rev_id DESC);

-- Per name and valid_from its latest revision (a withdrawal included): the entries in force as the owner sees them.
CREATE VIEW manual_cds_curve AS
SELECT DISTINCT ON (name_key, valid_from) *
FROM manual_cds_rev
ORDER BY name_key, valid_from, rev_id DESC;

CREATE TRIGGER manual_cds_rev_append_only BEFORE UPDATE OR DELETE ON manual_cds_rev
    FOR EACH ROW EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER manual_cds_rev_no_truncate BEFORE TRUNCATE ON manual_cds_rev
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
