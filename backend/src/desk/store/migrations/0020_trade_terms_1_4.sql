-- private/1.4 (08 §10 trade_terms): swap legs, REPO and HTM purchases. The typed view gains their fields as columns
-- after the 1.2 ones (CREATE OR REPLACE VIEW only appends), so existing queries keep working; every field stays in
-- data as well. Numbered 0020: 0019 is taken by a branch in flight.
CREATE OR REPLACE VIEW trade_terms_row AS
SELECT s.set_id, s.valid_date, s.source, s.revision, r.row_no, r.data->>'trade_id' AS trade_id,
       r.data->>'leg_id' AS leg_id, r.data->>'book_id' AS book_id, r.data->>'product_kind' AS product_kind,
       (r.data->>'trade_date')::date AS trade_date, (r.data->>'value_date')::date AS value_date,
       r.data->>'base_ccy' AS base_ccy, r.data->>'quote_ccy' AS quote_ccy, r.data->>'side' AS side,
       (r.data->>'notional_base')::numeric AS notional_base, (r.data->>'notional_quote')::numeric AS notional_quote,
       (r.data->>'contract_rate')::numeric AS contract_rate, coalesce((r.data->>'ndf')::boolean, false) AS ndf,
       r.data->>'settle_ccy' AS settle_ccy, (r.data->>'fixing_date')::date AS fixing_date,
       r.data->>'fixing_source' AS fixing_source, r.data->>'counterparty_id' AS counterparty_id,
       (r.data->>'pv_rub')::numeric AS pv_rub, r.data, r.src, r.x,
       r.data->>'entity' AS entity, r.data->>'position_id' AS position_id,
       r.data->>'ccy' AS ccy, (r.data->>'notional_ccy')::numeric AS notional_ccy,
       (r.data->>'start_date')::date AS start_date, (r.data->>'maturity_date')::date AS maturity_date,
       (r.data->>'fixed_rate_pct')::numeric AS fixed_rate_pct, r.data->>'index_code' AS index_code,
       (r.data->>'spread_bp')::numeric AS spread_bp, r.data->>'payment_frequency' AS payment_frequency,
       r.data->>'day_count' AS day_count, r.data->>'notional_exchange' AS notional_exchange,
       r.data->>'source_instrument_id' AS source_instrument_id, r.data->>'instrument_id' AS instrument_id,
       r.data->>'isin' AS isin, r.data->>'regnum' AS regnum, r.data->>'moex_secid' AS moex_secid,
       (r.data->>'quantity')::numeric AS quantity, (r.data->>'face_amount_ccy')::numeric AS face_amount_ccy,
       (r.data->>'purchase_price_pct')::numeric AS purchase_price_pct,
       (r.data->>'purchase_yield_pct')::numeric AS purchase_yield_pct,
       (r.data->>'contract_rate_pct')::numeric AS contract_rate_pct,
       (r.data->>'purchase_accrued_ccy')::numeric AS purchase_accrued_ccy,
       r.data->>'source_instrument_name' AS source_instrument_name
FROM dataset_row r JOIN dataset_set s USING (set_id) WHERE s.dataset = 'trade_terms';
