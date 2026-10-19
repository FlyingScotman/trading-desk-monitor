-- Positions of the private contract v1 (slice S7a, 08 §10): a bank-report row may have no resolved
-- instrument (an unmapped bond, an FX or OTC row: secid null) or give only a face amount (quantity null);
-- the published position then has secid null too, and quantity null when it is no number of bonds (an FX row,
-- an unmapped bond given by face); its fact says what it is.
ALTER TABLE position_input ALTER COLUMN secid DROP NOT NULL;
ALTER TABLE position_input ALTER COLUMN quantity DROP NOT NULL;
ALTER TABLE run_position ALTER COLUMN secid DROP NOT NULL;
ALTER TABLE run_position ALTER COLUMN quantity DROP NOT NULL;
