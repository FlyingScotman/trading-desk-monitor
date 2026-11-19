-- How a user wants the numbers of a position-table column shown (TDM#789): the digits after the point and the units
-- (тыс / mm / bn), per user of the monitor and per column. One format of a column holds in every position table of every
-- screen that has the column (not per table, unlike ``user_table_columns``, migrations 0025 and 0027).
--
-- ``username`` is the monitor's own account name (``app_user.username``) or ``local`` (this computer, sign-in off:
-- ``desk.api.auth.LOCAL``, which has no ``app_user`` row), so there is no foreign key: deleting a user removes their rows
-- in ``desk.api.auth.delete_user``. ``col_key`` is the key of the column in the page's catalog ``KIT.posCols`` (``mv``,
-- ``dv01``, ``carry``); the server knows only the shape of the key, a key the page does not know is ignored by it.
--
-- ``decimals`` 0..6 and ``scale`` ('1' as is, 'k' thousands, 'mm' millions, 'bn' billions) are each NULL = the column's
-- own default. Both NULL is not kept: no row = the default format. Not append-only: the user changes it back and forth,
-- only the latest state matters (a display preference, not a change of the monitor: no history, no audit rows).
CREATE TABLE user_column_formats (
    username    text NOT NULL CHECK (username ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
    col_key     text NOT NULL CHECK (col_key ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$'),
    decimals    smallint CHECK (decimals BETWEEN 0 AND 6),
    scale       text CHECK (scale IN ('1', 'k', 'mm', 'bn')),
    updated_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (username, col_key),
    CHECK (decimals IS NOT NULL OR scale IS NOT NULL)
);
