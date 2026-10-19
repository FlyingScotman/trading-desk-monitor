-- Scenarios (slice S6): definitions, exact (T2) repricing jobs and their results.
-- A definition is the prototype shape, normalized (desk.scenario.definition.normalize): curves are
-- 11 bp shifts per RISK_BUCKETS pillar of a published curve (only "RUB OFZ", the KBD, so far);
-- ofzSpread is the OFZ Z-spread shift to the KBD (not the prototype's spread to RUONIA); spreads
-- (_all, _mult, sector, issuer, rating) shift credit spreads; fx is % per currency against RUB.

-- Reference data, mutable: presets change by later migrations.
CREATE TABLE scenario_def (
    scenario_id text PRIMARY KEY,
    name        text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('preset', 'historical', 'custom')),
    description text,
    ord         integer NOT NULL DEFAULT 0,
    definition  jsonb NOT NULL,
    active      boolean NOT NULL DEFAULT true
);

-- One job per (run, normalized definition, method, core version) unless it failed: a resubmission by the
-- same code reuses the live or done job; a new method (desk.scenario.exact.METHOD) or core gets a new job.
CREATE TABLE scenario_job (
    job_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    base_run_id     bigint NOT NULL REFERENCES run,
    definition_hash text NOT NULL,
    definition      jsonb NOT NULL,          -- normalized
    status          text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    requested_at    timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    error           text,
    method          text NOT NULL,           -- of the submitting code; only a worker with the same runs it
    core_version    text NOT NULL,
    backend_version text                     -- of the worker that ran it
);
CREATE UNIQUE INDEX scenario_job_live ON scenario_job (base_run_id, definition_hash, method, core_version)
    WHERE status <> 'failed';
CREATE INDEX scenario_job_queue ON scenario_job (job_id) WHERE status = 'queued';

CREATE TABLE scenario_result (
    job_id          bigint PRIMARY KEY REFERENCES scenario_job,
    base_run_id     bigint NOT NULL REFERENCES run,
    definition_hash text NOT NULL,
    result          jsonb NOT NULL           -- desk.scenario.exact.reprice_run
);

-- Results are append-only; a job's status changes but a job is never deleted.
CREATE TRIGGER scenario_result_append_only BEFORE UPDATE OR DELETE ON scenario_result
    FOR EACH ROW EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER scenario_result_no_truncate BEFORE TRUNCATE ON scenario_result
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER scenario_job_no_delete BEFORE DELETE ON scenario_job
    FOR EACH ROW EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER scenario_job_no_truncate BEFORE TRUNCATE ON scenario_job
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();

-- The prototype presets (prototypes/data.js), on "RUB OFZ"; 'basis' is dropped (no key-rate curve in runs).
INSERT INTO scenario_def (scenario_id, name, kind, ord, definition) VALUES
('par+100', 'Параллельный +100 б.п. (КБД)', 'preset', 10,
 '{"curves": {"RUB OFZ": [100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0]}}'),
('par-100', 'Параллельный −100 б.п. (КБД)', 'preset', 20,
 '{"curves": {"RUB OFZ": [-100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0]}}'),
('keyhike', 'Повышение КС +200 б.п.', 'preset', 30,
 '{"curves": {"RUB OFZ": [200.0, 195.0, 180.0, 150.0, 105.0, 85.0, 65.0, 55.0, 45.0, 40.0, 35.0]},
   "ofzSpread": 10.0, "spreads": {"_all": 25.0}}'),
('bullsteep', 'Bull steepener (смягчение ДКП)', 'preset', 40,
 '{"curves": {"RUB OFZ": [-160.0, -150.0, -130.0, -105.0, -75.0, -60.0, -45.0, -35.0, -25.0, -20.0, -15.0]},
   "ofzSpread": 5.0}'),
('bearflat', 'Bear flattener', 'preset', 50,
 '{"curves": {"RUB OFZ": [110.0, 100.0, 90.0, 75.0, 50.0, 35.0, 20.0, 12.0, 8.0, 5.0, 5.0]}, "ofzSpread": 5.0}'),
('ofzwide', 'Спред ОФЗ к КБД +40 б.п.', 'preset', 60, '{"ofzSpread": 40.0}'),
('rubdep', 'Ослабление рубля −15%', 'preset', 70,
 '{"curves": {"RUB OFZ": [50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0]}, "ofzSpread": 10.0,
   "fx": {"CHF": 15.0, "CNY": 15.0, "EUR": 15.0, "USD": 15.0}}'),
('credit', 'Кредитный стресс: спреды ×2', 'preset', 80, '{"ofzSpread": 40.0, "spreads": {"_mult": 2.0}}'),
('sector', 'Стресс девелоперов и лизинга', 'preset', 90,
 '{"spreads": {"sector": {"Девелопмент": 300.0, "Лизинг": 150.0}, "rating": {"A": 80.0, "BB": 250.0}}}'),
('stress22', 'Стресс «2022»', 'preset', 100,
 '{"curves": {"RUB OFZ": [1000.0, 950.0, 800.0, 650.0, 480.0, 400.0, 320.0, 270.0, 240.0, 220.0, 200.0]},
   "ofzSpread": -20.0, "fx": {"CHF": 32.0, "CNY": 30.0, "EUR": 32.0, "USD": 35.0}, "spreads": {"_mult": 2.5}}');
