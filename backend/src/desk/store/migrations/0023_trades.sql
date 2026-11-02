-- private/1.6 (08 §10 trades, S13a, TDM#525): the day's trades from the private adapter, stored generically like the
-- other datasets (dataset_set / dataset_row). A trades source is either a cumulative export (full_snapshot per book:
-- its current set replaces the book's trades of the date) or deltas (increment: its sets add to the date's trades).
-- The set records which, so a read knows whether a set replaces or adds; null is a set stored before this migration,
-- always a full snapshot. Reads: store.dataset_rows("trades", D), the current version of each trade.
ALTER TABLE dataset_set ADD COLUMN completeness text;

-- Typed view of every stored trade version with its set (the current ones: store.dataset_rows). trade_ts stays text:
-- one without an offset is Moscow time (08 §6.5), which a cast in the session's time zone would not know.
CREATE VIEW trade_row AS
SELECT s.set_id, s.valid_date, s.source, s.revision, s.report_ts, s.completeness, r.row_no,
       r.data->>'trade_id' AS trade_id, (r.data->>'trade_version')::bigint AS trade_version,
       r.data->>'status' AS status, (r.data->>'trade_date')::date AS trade_date,
       r.data->>'trade_ts' AS trade_ts, r.data->>'book_id' AS book_id,
       r.data->>'position_id' AS position_id, r.data->>'asset_class' AS asset_class,
       r.data->>'source_instrument_id' AS source_instrument_id, r.data->>'instrument_id' AS instrument_id,
       r.data->>'isin' AS isin, r.data->>'side' AS side, (r.data->>'quantity')::numeric AS quantity,
       (r.data->>'face_amount_ccy')::numeric AS face_amount_ccy, (r.data->>'amount_ccy')::numeric AS amount_ccy,
       (r.data->>'price_pct')::numeric AS price_pct, (r.data->>'fx_rate')::numeric AS fx_rate,
       (r.data->>'accrued_ccy')::numeric AS accrued_ccy, (r.data->>'settle_date')::date AS settle_date,
       r.data->>'counterparty_id' AS counterparty_id, r.data->>'ccy' AS ccy, r.data, r.src, r.x
FROM dataset_row r JOIN dataset_set s USING (set_id) WHERE s.dataset = 'trades';
