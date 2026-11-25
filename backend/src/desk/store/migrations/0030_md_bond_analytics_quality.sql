-- TDM#766. Hide unreliable MOEX analytics even for quotes ingested before the source published its flags.
ALTER TABLE md_bond_eod
    ADD COLUMN IF NOT EXISTS effective_yield_unreliable boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS md_bond_eod_unreliable ON md_bond_eod (business_date, secid)
    WHERE effective_yield_unreliable;

-- An older retained static can still describe a bond that has matured and left the newest snapshot.
CREATE OR REPLACE VIEW md_bond_analytics_static AS
SELECT DISTINCT ON (b.secid) b.secid, b.snapshot_id, s.business_date,
       (b.data->>'effective_yield_unreliable')::boolean AS unreliable
  FROM md_static_bond b JOIN md_static_summary s USING (snapshot_id)
 WHERE b.data ? 'effective_yield_unreliable'
 ORDER BY b.secid, s.business_date DESC, s.loaded_at DESC, s.snapshot_id DESC;

CREATE OR REPLACE VIEW md_bond_day AS
WITH quotes AS (
    SELECT h.trade_date AS business_date, h.secid, h.board, h.clean_pct, h.price_basis, h.yield_pct, h.yield_date,
           h.duration_days, h.gspread_bp, h.zspread_bp, h.accrued_per_bond, h.face_value, h.volume_bonds, h.value,
           h.currency_id, h.num_trades, h.icpi, h.ir, h.bei, h.cbr, 'hub'::text AS origin,
           e.secid IS NOT NULL AS unreliable
      FROM md_hub_bond h
      LEFT JOIN md_bond_eod e ON e.business_date = h.trade_date AND e.secid = h.secid AND e.effective_yield_unreliable
    UNION ALL
    SELECT b.business_date, b.secid, b.board, b.clean_pct, b.price_basis, b.yield_pct, b.yield_date, b.duration_days,
           b.gspread_bp, b.zspread_bp, b.accrued_per_bond, b.face_value, b.volume_bonds, b.value, b.currency_id,
           b.num_trades, b.icpi, b.ir, b.bei, b.cbr, 'snapshot'::text, b.effective_yield_unreliable
      FROM md_bond_eod b
     WHERE NOT EXISTS (SELECT 1 FROM md_hub_bond h WHERE h.trade_date = b.business_date AND h.secid = b.secid)
), classified AS (
    SELECT q.*, q.unreliable OR COALESCE(s.unreliable, false) AS effective_yield_unreliable
      FROM quotes q LEFT JOIN md_bond_analytics_static s USING (secid)
)
SELECT business_date, secid, board, clean_pct, price_basis,
       CASE WHEN NOT effective_yield_unreliable THEN yield_pct END AS yield_pct,
       CASE WHEN NOT effective_yield_unreliable THEN yield_date END AS yield_date,
       CASE WHEN NOT effective_yield_unreliable THEN duration_days END AS duration_days,
       CASE WHEN NOT effective_yield_unreliable THEN gspread_bp END AS gspread_bp,
       CASE WHEN NOT effective_yield_unreliable THEN zspread_bp END AS zspread_bp,
       accrued_per_bond, face_value, volume_bonds, value, currency_id,
       num_trades, icpi, ir, bei, cbr, origin, effective_yield_unreliable
  FROM classified;
