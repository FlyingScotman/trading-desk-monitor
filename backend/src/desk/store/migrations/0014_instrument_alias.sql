-- Instrument aliases (batch 1, item 3): a display name a user gives a security by its ISIN, for the exchange names
-- that say little (FX-linked OFZ, Russian eurobonds). Not part of a run's frozen rows: it is joined at read time,
-- so an alias shows on every run, old ones too, and clearing it brings the exchange name back.
-- Append-only: each change is a revision with who and when; the alias in force is the ISIN's latest revision, and a
-- revision with a null alias clears it. changed_by is 'local' (a loopback client, api.alias_routes) until users
-- and roles exist; then the user's name.
CREATE TABLE instrument_alias_rev (
    rev_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    isin       text NOT NULL CHECK (isin ~ '^[A-Z]{2}[A-Z0-9]{9}[0-9]$'),
    alias      text CHECK (alias IS NULL OR (alias = btrim(alias) AND char_length(alias) BETWEEN 1 AND 120)),
    changed_by text NOT NULL DEFAULT 'local',
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX instrument_alias_rev_by_isin ON instrument_alias_rev (isin, rev_id DESC);

-- The aliases in force: per ISIN its latest revision, unless that one cleared it.
CREATE VIEW instrument_alias AS
SELECT isin, alias, changed_by, changed_at, rev_id
FROM (SELECT DISTINCT ON (isin) * FROM instrument_alias_rev ORDER BY isin, rev_id DESC) latest
WHERE alias IS NOT NULL;

CREATE TRIGGER instrument_alias_rev_append_only BEFORE UPDATE OR DELETE ON instrument_alias_rev
    FOR EACH ROW EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER instrument_alias_rev_no_truncate BEFORE TRUNCATE ON instrument_alias_rev
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
