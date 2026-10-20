-- Presets on the runs' curves (R8): a run publishes the risk per curve, "RUB OFZ" (the KBD), "RUB RUONIA" and
-- "RUB KEY" (a floater's projection curves, core RUB_RUONIA_OIS and RUB_KEY_RATE_PROJ), and a currency's curve
-- ("USD"...). Each curve moves only by its own vector (desk.scenario.exact.core_scenario), so a move of all rates
-- shifts the three RUB curves alike, as the prototype presets do (prototypes/data.js); a shock of one curve only (the
-- basis preset, a custom KBD shock) is the basis risk. "RUB KEY" of the 2022 stress is the prototype's (above RUONIA).
-- The definitions stay normalized (desk.scenario.definition.normalize): eleven floats per curve.
UPDATE scenario_def SET name = 'Параллельный +100 б.п. (RUB)', definition =
 '{"curves": {"RUB OFZ": [100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
              "RUB RUONIA": [100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
              "RUB KEY": [100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0]}}'
 WHERE scenario_id = 'par+100';
UPDATE scenario_def SET name = 'Параллельный −100 б.п. (RUB)', definition =
 '{"curves": {"RUB OFZ": [-100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0],
              "RUB RUONIA": [-100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0],
              "RUB KEY": [-100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0]}}'
 WHERE scenario_id = 'par-100';
UPDATE scenario_def SET definition =
 '{"curves": {"RUB OFZ": [200.0, 195.0, 180.0, 150.0, 105.0, 85.0, 65.0, 55.0, 45.0, 40.0, 35.0],
              "RUB RUONIA": [200.0, 195.0, 180.0, 150.0, 105.0, 85.0, 65.0, 55.0, 45.0, 40.0, 35.0],
              "RUB KEY": [200.0, 195.0, 180.0, 150.0, 105.0, 85.0, 65.0, 55.0, 45.0, 40.0, 35.0]},
   "ofzSpread": 10.0, "spreads": {"_all": 25.0}}'
 WHERE scenario_id = 'keyhike';
UPDATE scenario_def SET definition =
 '{"curves": {"RUB OFZ": [-160.0, -150.0, -130.0, -105.0, -75.0, -60.0, -45.0, -35.0, -25.0, -20.0, -15.0],
              "RUB RUONIA": [-160.0, -150.0, -130.0, -105.0, -75.0, -60.0, -45.0, -35.0, -25.0, -20.0, -15.0],
              "RUB KEY": [-160.0, -150.0, -130.0, -105.0, -75.0, -60.0, -45.0, -35.0, -25.0, -20.0, -15.0]},
   "ofzSpread": 5.0}'
 WHERE scenario_id = 'bullsteep';
UPDATE scenario_def SET definition =
 '{"curves": {"RUB OFZ": [110.0, 100.0, 90.0, 75.0, 50.0, 35.0, 20.0, 12.0, 8.0, 5.0, 5.0],
              "RUB RUONIA": [110.0, 100.0, 90.0, 75.0, 50.0, 35.0, 20.0, 12.0, 8.0, 5.0, 5.0],
              "RUB KEY": [110.0, 100.0, 90.0, 75.0, 50.0, 35.0, 20.0, 12.0, 8.0, 5.0, 5.0]}, "ofzSpread": 5.0}'
 WHERE scenario_id = 'bearflat';
UPDATE scenario_def SET definition =
 '{"curves": {"RUB OFZ": [50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0],
              "RUB RUONIA": [50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0],
              "RUB KEY": [50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0]}, "ofzSpread": 10.0,
   "fx": {"CHF": 15.0, "CNY": 15.0, "EUR": 15.0, "USD": 15.0}}'
 WHERE scenario_id = 'rubdep';
UPDATE scenario_def SET definition =
 '{"curves": {"RUB OFZ": [1000.0, 950.0, 800.0, 650.0, 480.0, 400.0, 320.0, 270.0, 240.0, 220.0, 200.0],
              "RUB RUONIA": [1000.0, 950.0, 800.0, 650.0, 480.0, 400.0, 320.0, 270.0, 240.0, 220.0, 200.0],
              "RUB KEY": [1050.0, 1000.0, 850.0, 680.0, 500.0, 410.0, 330.0, 280.0, 250.0, 230.0, 210.0]},
   "ofzSpread": -20.0, "fx": {"CHF": 32.0, "CNY": 30.0, "EUR": 32.0, "USD": 35.0}, "spreads": {"_mult": 2.5}}'
 WHERE scenario_id = 'stress22';
INSERT INTO scenario_def (scenario_id, name, kind, ord, definition) VALUES
('basis', 'Базис КС − RUONIA +50 б.п.', 'preset', 55,
 '{"curves": {"RUB KEY": [50.0, 50.0, 45.0, 40.0, 30.0, 25.0, 20.0, 15.0, 10.0, 10.0, 10.0]}}');
