-- R18: the OFZ-RUONIA basis is a factor of the runs ("RUB OFZ-RUONIA": OFZ curve = RUONIA + basis). A preset that
-- widens it alone: the OFZ curve moves by the basis, RUONIA and the corporates stay (desk.scenario.definition
-- curve_moves). The other presets move "RUB OFZ" and "RUB RUONIA" alike, so their basis stays.
INSERT INTO scenario_def (scenario_id, name, kind, ord, definition) VALUES
('ofzbasis', 'Базис ОФЗ − RUONIA +50 б.п.', 'preset', 56,
 '{"curves": {"RUB OFZ-RUONIA": [50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0]}}');
