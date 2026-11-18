-- Which columns of a position table a user has hidden (TDM#763): per user of the monitor, per table and set.
--
-- ``username`` is the monitor's own account name (``app_user.username``) or ``local`` (this computer, sign-in off:
-- ``desk.api.auth.LOCAL``, which has no ``app_user`` row), so there is no foreign key: deleting a user removes
-- their rows in ``desk.api.auth.delete_user``. ``table_key`` is screen + table (+ the column set, where a screen has
-- sets), e.g. ``c01-positions`` or ``c03-blotter:base``; the pages build it (``KIT.colKey``).
--
-- Only the HIDDEN columns are stored: a column that appears in a later version of a table is shown by default, and a
-- key this version does not know is ignored by the page. No row = the table's default (every column of its set).
-- Not append-only: the user changes it back and forth, only the latest state matters (no history, no audit rows:
-- it is a display preference, not a change of the monitor).
CREATE TABLE user_table_columns (
    username    text NOT NULL CHECK (username ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
    table_key   text NOT NULL CHECK (table_key ~ '^[a-z0-9][a-z0-9._:-]{0,127}$'),
    hidden      jsonb NOT NULL CHECK (jsonb_typeof(hidden) = 'array'),  -- the column keys, a list of strings
    updated_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (username, table_key)
);
