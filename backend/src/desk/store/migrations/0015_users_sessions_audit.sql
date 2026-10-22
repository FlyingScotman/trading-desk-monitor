-- Users, sessions and the audit log (batch 1, item 9; 05 §6, DECISIONS DEC13): the monitor's own accounts once it is
-- opened from other computers (api/auth.py). Auth is off by default (desk serve on 127.0.0.1, as before); these
-- tables are used when it is on, and by the users section of the settings page and `desk user ...` either way.
--
-- A password is stored only as its Argon2id hash (argon2-cffi, ADR04: no custom cryptography). A session is a
-- random token in an httpOnly cookie; the store keeps its SHA-256 only, so a copy of the database cannot be replayed
-- as a cookie. Roles (05 §6): viewer reads; trader also submits scenario jobs, runs the autopilot and edits aliases;
-- admin also changes settings, the adapter, updates and users.
CREATE TABLE app_user (
    user_id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username              text NOT NULL CHECK (username ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
    display_name          text CHECK (display_name IS NULL OR char_length(display_name) BETWEEN 1 AND 120),
    role                  text NOT NULL CHECK (role IN ('viewer', 'trader', 'admin')),
    password_hash         text NOT NULL,
    active                boolean NOT NULL DEFAULT true,
    must_change_password  boolean NOT NULL DEFAULT false,
    failed_logins         integer NOT NULL DEFAULT 0,  -- consecutive, reset by a good login
    locked_until          timestamptz,                 -- the lockout after too many failures
    created_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by            text NOT NULL,
    updated_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
    password_changed_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_login_at         timestamptz,
    UNIQUE (username)
);

CREATE TABLE app_session (
    session_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    token_sha256  text NOT NULL UNIQUE,  -- hex SHA-256 of the cookie's token
    user_id       bigint NOT NULL REFERENCES app_user ON DELETE CASCADE,
    csrf_token    text NOT NULL,         -- sent back in X-CSRF-Token by the pages' state-changing requests
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at    timestamptz NOT NULL,  -- absolute; the idle limit is last_seen_at + session_idle_minutes
    revoked_at    timestamptz,           -- logout, a disabled user, a password reset
    ip            text,
    user_agent    text
);
CREATE INDEX app_session_by_user ON app_session (user_id) WHERE revoked_at IS NULL;

-- Who did what: logins (and failures), logouts, user changes, settings / adapter / update changes, alias edits.
-- Append-only like the runs: no update, delete or truncate.
CREATE TABLE audit_log (
    audit_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    actor     text NOT NULL,  -- a username; 'local' (this computer, auth off); 'cli:<os user>' (desk user ...)
    action    text NOT NULL,  -- login, login_failed, logout, user_add, user_change, password_reset, settings, ...
    target    text,           -- the user changed, the request path, the ISIN
    ok        boolean NOT NULL DEFAULT true,
    ip        text,
    detail    jsonb           -- never a password, token or setting value: names and statuses only
);
CREATE INDEX audit_log_at ON audit_log (at DESC);

CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION desk_forbid_change();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION desk_forbid_change();
