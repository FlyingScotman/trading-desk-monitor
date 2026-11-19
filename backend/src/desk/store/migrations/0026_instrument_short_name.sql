-- Short names of securities (TDM#776): the name a user gives a security, by its ISIN, for the narrow columns and the
-- card's «Короткое имя», apart from the full name (the alias of 0014). Without one the exchange's short name shows
-- (the hub's MOEX SHORTNAME, in a run's rows as fact.extra.short_name).
-- A table of its own, not a column of instrument_alias_rev: the two names are separate, the alias table and its view
-- (read by the positions route) stay as they are, and nothing existing is rewritten. Same shape as 0014:
-- append-only revisions with who and when, the name in force is the ISIN's latest revision, a revision with a null
-- name clears it; changed_by is 'local' (a loopback client) while sign-in is off, then the user's name.
-- Not part of a run's frozen rows: joined at read time, so it shows on every run, old ones too. A backup (pg_dump of
-- the whole store, desk.backup) takes it with the other tables.
CREATE TABLE instrument_short_name_rev (
    rev_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    isin       text NOT NULL CHECK (isin ~ '^[A-Z]{2}[A-Z0-9]{9}[0-9]$'),
    short_name text CHECK (short_name IS NULL OR (short_name = btrim(short_name) AND char_length(short_name) BETWEEN 1 AND 40)),
    changed_by text NOT NULL DEFAULT 'local',
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX instrument_short_name_rev_by_isin ON instrument_short_name_rev (isin, rev_id DESC);

-- The short names in force: per ISIN its latest revision, unless that one cleared it.
CREATE VIEW instrument_short_name AS
SELECT isin, short_name, changed_by, changed_at, rev_id
FROM (SELECT DISTINCT ON (isin) * FROM instrument_short_name_rev ORDER BY isin, rev_id DESC) latest
WHERE short_name IS NOT NULL;

CREATE TRIGGER instrument_short_name_rev_append_only BEFORE UPDATE OR DELETE ON instrument_short_name_rev
    FOR EACH ROW EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER instrument_short_name_rev_no_truncate BEFORE TRUNCATE ON instrument_short_name_rev
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
