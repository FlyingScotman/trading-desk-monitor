-- private/1.3 (08 §10 positions): the accrued interest paid at purchase of an HTM position, in the row's currency.
-- The funding engine funds it until the coupon date that returns it when the organisation's HTM base is the cash
-- spent (funding_policy.htm_base = cash_spent, OQ-02); a 1.0-1.2 row has none.
ALTER TABLE position_input ADD COLUMN purchase_accrued_ccy numeric;
