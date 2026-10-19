-- Optional risk fields of the private contract v1 positions (08 §10): the report's own maturity, DV01 and CS01
-- of a row the backend does not value itself (otc, other, an unmapped bond on the reported_value rung).
ALTER TABLE position_input
    ADD COLUMN maturity_date  date,
    ADD COLUMN risk_dv01_rub  numeric,
    ADD COLUMN risk_cs01_rub  numeric;
