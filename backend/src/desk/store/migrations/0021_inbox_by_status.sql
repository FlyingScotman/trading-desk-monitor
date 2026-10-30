-- The units and issue codes of a file's reading are looked up by its status row (day status C14, the files of a dataset
-- and of the report folders, TDM#445): without these an inbox of years of reports reads both tables whole per request.
CREATE INDEX inbox_unit_by_status ON inbox_unit (status_id);
CREATE INDEX inbox_issue_by_status ON inbox_issue (status_id);
