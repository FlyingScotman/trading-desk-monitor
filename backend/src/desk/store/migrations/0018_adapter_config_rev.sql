-- Revisions of the private adapter's config files (08 §4 settings_tabs, FND-4): what the settings page wrote to
-- <config folder>/<file>, with who and when, so a book, entity or funding change can be looked at and undone.
-- Append-only, like instrument_alias_rev. A revision per write from the page (via 'form' or 'raw'); before such a
-- write, a file changed outside the page since its last revision (or never recorded) gets a revision of its
-- previous content with via 'file' and changed_by 'file'. changed_by is 'local' (a loopback client) until users and
-- roles exist; then the user's name. The files stay the source of truth: the adapter reads them, not this table.
CREATE TABLE adapter_config_rev (
    rev_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    config_dir     text NOT NULL,                 -- the folder as the settings resolved it
    file_name      text NOT NULL,
    content        text NOT NULL,
    content_sha256 text NOT NULL,
    via            text NOT NULL CHECK (via IN ('form', 'raw', 'file')),
    tab_id         text,                          -- the settings tab of a form write
    changed_by     text NOT NULL DEFAULT 'local',
    changed_at     timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX adapter_config_rev_by_file ON adapter_config_rev (config_dir, file_name, rev_id DESC);

CREATE TRIGGER adapter_config_rev_append_only BEFORE UPDATE OR DELETE ON adapter_config_rev
    FOR EACH ROW EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER adapter_config_rev_no_truncate BEFORE TRUNCATE ON adapter_config_rev
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
