-- Columns a user has ADDED to a position table from the catalog of the standard position columns (TDM#778), next to the
-- hidden ones of migration 0025: ``added`` is the list of column keys in the order they were added (they stand after the
-- table's own columns). A key this version does not know is ignored by the page; a column that a later version makes a
-- column of the table itself is shown once, as the table's own. An old row reads as ``[]`` (nothing added), so what a user
-- hid before keeps working. A row with nothing hidden and nothing added is not kept: no row = the table's default.
ALTER TABLE user_table_columns
    ADD COLUMN added jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(added) = 'array');
