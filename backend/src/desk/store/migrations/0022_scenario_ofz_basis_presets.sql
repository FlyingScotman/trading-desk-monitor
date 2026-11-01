-- The presets of the OFZ basis (TDM#467): the KBD's level moves with RUONIA held, i.e. "RUB OFZ" alone (the OFZ valued in
-- RUONIA + basis moves by its basis, a KBD floater by its projection; a RUB RUONIA bond does not move). The 'basis' preset
-- is the key rate against RUONIA.
INSERT INTO scenario_def (scenario_id, name, kind, ord, definition) VALUES
('ofzbasis+25', 'Базис ОФЗ к RUONIA +25 б.п.', 'preset', 56,
 '{"curves": {"RUB OFZ": [25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0]}}'),
('ofzbasis-25', 'Базис ОФЗ к RUONIA −25 б.п.', 'preset', 57,
 '{"curves": {"RUB OFZ": [-25.0, -25.0, -25.0, -25.0, -25.0, -25.0, -25.0, -25.0, -25.0, -25.0, -25.0]}}')
ON CONFLICT (scenario_id) DO NOTHING;
