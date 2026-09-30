-- 0007_accounts_sessions_events: sign-in, sessions, devices and the
-- event-sourced tenant log of the production API (§44, §52, §55).
--
-- The API keeps each tenant's back office in memory and rebuilds it by
-- replaying tenant_events, an append-only, hash-chained log of every change
-- (backend/src/backoffice/server). Sign-in data lives next to it:
--
--   user_credentials   scrypt password hashes, readable only by their user
--   sessions           SHA-256 of the session token, never the token
--   login_attempts     rate limiting; subjects are keyed hashes, never an
--                      email address or an IP address
--   devices            Expo push tokens (rare notifications, §42)
--   accountant_api_keys  fingerprint -> tenant for the accountant API (§28)
--   oauth_nonces       single-use mailbox sign-ins in progress (§47)
--   tenant_erasures    permanent record that an account was erased (§52),
--                      without personal data
--
-- Identity tables that belong to no tenant still have row-level security,
-- ENABLED and FORCED, keyed on what the caller presents: the signed-in user
-- (app.user_id), the email being signed in (app.login_email), the session
-- token hash (app.session_hash), the rate-limit subjects (app.rate_subjects)
-- or the API key fingerprint (app.api_key_hash). With none set, nothing is
-- visible (fail closed).

-- --------------------------------------------------------------------- roles

-- Admins run the internal dashboard (a membership role, like owner).
ALTER DOMAIN membership_role DROP CONSTRAINT membership_role_check;
ALTER DOMAIN membership_role ADD CONSTRAINT membership_role_check
    CHECK (VALUE IN ('owner', 'accountant', 'admin'));

-- --------------------------------------------------------------------- users and tenants

-- Sign-in finds the account by the email typed in; nothing else is readable that way.
CREATE POLICY login_lookup ON users FOR SELECT
    USING (email = current_setting('app.login_email', true));
-- Account deletion (§52): a person may delete their own user row.
CREATE POLICY self_delete ON users FOR DELETE
    USING (id = current_setting('app.user_id', true));

GRANT DELETE ON users, memberships, tenants TO backoffice_app;

CREATE TABLE user_credentials (
    user_id        bo_id       PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    -- "scrypt$n$r$p$salt$hash" (or an argon2id PHC string); never a plain password.
    password_hash  text        NOT NULL CHECK (password_hash ~ '^(scrypt\$[0-9]+\$[0-9]+\$[0-9]+\$[A-Za-z0-9_-]+\$[A-Za-z0-9_-]+|\$argon2id\$.+)$'),
    changed_at     timestamptz NOT NULL
);

COMMENT ON TABLE user_credentials IS
    'Password hashes (scrypt n=2^15 r=8 p=1). Readable and writable only by their own user (app.user_id).';

ALTER TABLE user_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_credentials FORCE ROW LEVEL SECURITY;
CREATE POLICY own_credentials ON user_credentials
    USING (user_id = current_setting('app.user_id', true))
    WITH CHECK (user_id = current_setting('app.user_id', true));

GRANT SELECT, INSERT ON user_credentials TO backoffice_app;
GRANT UPDATE (password_hash, changed_at) ON user_credentials TO backoffice_app;

-- --------------------------------------------------------------------- sessions

CREATE TABLE sessions (
    token_hash    sha256_hex  PRIMARY KEY,   -- SHA-256 of the random token; the token itself is never stored
    user_id       bo_id       NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    tenant_id     tenant_key  NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    client        text        NOT NULL CHECK (client IN ('web', 'mobile')),
    created_at    timestamptz NOT NULL,
    last_seen_at  timestamptz NOT NULL,
    expires_at    timestamptz NOT NULL,
    revoked_at    timestamptz,
    CHECK (expires_at > created_at),
    CHECK (last_seen_at >= created_at),
    CHECK (revoked_at IS NULL OR revoked_at >= created_at)
);

COMMENT ON TABLE sessions IS
    'Signed-in sessions (30 days, sliding). Found by the hash of the token presented (app.session_hash).';

CREATE INDEX sessions_user_idx ON sessions (user_id);
CREATE INDEX sessions_tenant_idx ON sessions (tenant_id);
CREATE INDEX sessions_expires_idx ON sessions (expires_at);

ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY presented_token ON sessions
    USING (token_hash = current_setting('app.session_hash', true))
    WITH CHECK (token_hash = current_setting('app.session_hash', true));
CREATE POLICY tenant_isolation ON sessions
    USING (tenant_id = current_setting('app.tenant_id', true)
           AND user_id = current_setting('app.user_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)
                AND user_id = current_setting('app.user_id', true));

GRANT SELECT, INSERT, DELETE ON sessions TO backoffice_app;
GRANT UPDATE (last_seen_at, expires_at, revoked_at) ON sessions TO backoffice_app;

-- --------------------------------------------------------------------- login attempts

CREATE TABLE login_attempts (
    subject       sha256_hex  NOT NULL,   -- HMAC-SHA-256 of 'email:<address>' or 'ip:<address>'
    id            bigint      GENERATED ALWAYS AS IDENTITY,
    attempted_at  timestamptz NOT NULL,
    succeeded     boolean     NOT NULL,
    PRIMARY KEY (subject, id)
);

COMMENT ON TABLE login_attempts IS
    'Sign-in rate limiting (10 per 15 minutes per email and per IP). Keyed hashes only; kept one day.';

CREATE INDEX login_attempts_subject_at_idx ON login_attempts (subject, attempted_at);
CREATE INDEX login_attempts_at_idx ON login_attempts (attempted_at);

ALTER TABLE login_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE login_attempts FORCE ROW LEVEL SECURITY;
CREATE POLICY presented_subjects ON login_attempts
    USING (subject = ANY (string_to_array(current_setting('app.rate_subjects', true), ',')))
    WITH CHECK (subject = ANY (string_to_array(current_setting('app.rate_subjects', true), ',')));
-- Anything older than a day may be cleaned up by any caller (it only holds hashes).
CREATE POLICY expired_visible ON login_attempts FOR SELECT
    USING (attempted_at < now() - interval '1 day');
CREATE POLICY expired_removable ON login_attempts FOR DELETE
    USING (attempted_at < now() - interval '1 day');

GRANT SELECT, INSERT, DELETE ON login_attempts TO backoffice_app;

-- --------------------------------------------------------------------- devices

CREATE TABLE devices (
    tenant_id        tenant_key  NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    expo_push_token  text        NOT NULL CHECK (expo_push_token ~ '^Expo(nent)?PushToken\[[A-Za-z0-9_-]{8,200}\]$'),
    user_id          bo_id       NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    platform         text        NOT NULL CHECK (platform IN ('ios', 'android')),
    created_at       timestamptz NOT NULL,
    last_seen_at     timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, expo_push_token)
);

COMMENT ON TABLE devices IS 'Phones that receive the rare notifications (§42): hard approvals, reconnects, closed months.';

CREATE INDEX devices_user_idx ON devices (user_id);

CALL enable_tenant_isolation('devices');

GRANT SELECT, INSERT, DELETE ON devices TO backoffice_app;
GRANT UPDATE (user_id, platform, last_seen_at) ON devices TO backoffice_app;

-- --------------------------------------------------------------------- tenant event log

CREATE TABLE tenant_erasures (
    tenant_id          tenant_key  PRIMARY KEY,   -- no foreign key: the tenant row is erased
    requested_at       timestamptz NOT NULL,
    completed_at       timestamptz,
    events_erased      bigint      NOT NULL DEFAULT 0 CHECK (events_erased >= 0),
    objects_purged_at  timestamptz,
    CHECK (completed_at IS NULL OR completed_at >= requested_at)
);

COMMENT ON TABLE tenant_erasures IS
    'Owner-confirmed account erasures (§52). Holds no personal data; objects_purged_at NULL = evidence files still to purge.';

CALL enable_tenant_isolation('tenant_erasures');

GRANT SELECT, INSERT ON tenant_erasures TO backoffice_app;
GRANT UPDATE (completed_at, events_erased, objects_purged_at) ON tenant_erasures TO backoffice_app;

-- body is TEXT, not jsonb: the hashed bytes come back exactly as written
-- (canonical JSON, ASCII only). hash = SHA-256(prev_hash || '\n' || body);
-- the first event links to SHA-256('backoffice.events.v1:' || tenant_id).
CREATE TABLE tenant_events (
    tenant_id    tenant_key  NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    seq          bigint      NOT NULL CHECK (seq >= 1),
    at           timestamptz NOT NULL,
    kind         text        NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$'),
    actor        text        NOT NULL CHECK (actor ~ '^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$'),
    body         text        NOT NULL,
    prev_hash    sha256_hex  NOT NULL,
    hash         sha256_hex  NOT NULL,
    recorded_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, seq),
    UNIQUE (tenant_id, prev_hash),
    UNIQUE (hash),
    CHECK (hash <> prev_hash)
);

COMMENT ON TABLE tenant_events IS
    'Every change to a tenant, in order (event sourcing). Replaying it rebuilds the back office. Append-only.';

CREATE FUNCTION tenant_events_genesis_hash(p_tenant text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN encode(sha256(convert_to('backoffice.events.v1:' || p_tenant, 'UTF8')), 'hex');

COMMENT ON FUNCTION tenant_events_genesis_hash(text) IS 'prev_hash of a tenant''s first event.';

CREATE FUNCTION check_tenant_event_chain() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public
AS $$
DECLARE
    head tenant_events%ROWTYPE;
BEGIN
    IF NEW.hash::text <> encode(sha256(convert_to(NEW.prev_hash::text || E'\n' || NEW.body, 'UTF8')), 'hex') THEN
        RAISE EXCEPTION 'event % hash does not match its body', NEW.seq
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT * INTO head FROM tenant_events
    WHERE tenant_id = NEW.tenant_id
    ORDER BY seq DESC
    LIMIT 1;
    IF NOT FOUND THEN
        IF NEW.seq <> 1 OR NEW.prev_hash::text <> tenant_events_genesis_hash(NEW.tenant_id) THEN
            RAISE EXCEPTION 'the first event of a tenant must have seq 1 and the genesis hash'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.seq <> head.seq + 1 OR NEW.prev_hash <> head.hash THEN
        RAISE EXCEPTION 'event % does not extend the head %', NEW.seq, head.seq
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.at < head.at THEN
        RAISE EXCEPTION 'event % is dated before the head', NEW.seq
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$;

-- True inside an erasure transaction for the tenant being erased.
CREATE FUNCTION tenant_erasure_allowed(p_tenant text) RETURNS boolean
    LANGUAGE sql STABLE
    SET search_path = pg_catalog, public
    RETURN p_tenant IS NOT NULL
        AND current_setting('backoffice.tenant_erasure', true) = p_tenant
        AND EXISTS (SELECT 1 FROM tenant_erasures e WHERE e.tenant_id = p_tenant);

COMMENT ON FUNCTION tenant_erasure_allowed(text) IS
    'Events disappear only with their tenant, in the transaction that records the erasure (§52).';

CREATE FUNCTION guard_tenant_events() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public
AS $$
BEGIN
    IF TG_OP = 'DELETE' AND tenant_erasure_allowed(OLD.tenant_id) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'tenant_events is append-only: % is not allowed', TG_OP
        USING ERRCODE = 'restrict_violation',
              HINT = 'Record a new event; events are only removed when the account is erased.';
END
$$;

CREATE TRIGGER tenant_events_chain BEFORE INSERT ON tenant_events
    FOR EACH ROW EXECUTE FUNCTION check_tenant_event_chain();
CREATE TRIGGER tenant_events_append_only BEFORE UPDATE OR DELETE ON tenant_events
    FOR EACH ROW EXECUTE FUNCTION guard_tenant_events();
CREATE TRIGGER tenant_events_no_truncate BEFORE TRUNCATE ON tenant_events
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

CALL enable_tenant_isolation('tenant_events');

GRANT SELECT, INSERT ON tenant_events TO backoffice_app;
GRANT SELECT ON tenant_events, tenant_erasures TO backoffice_readonly;

-- --------------------------------------------------------------------- accountant API keys

CREATE TABLE accountant_api_keys (
    tenant_id   tenant_key  NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    key_id      bo_id       NOT NULL,
    key_hash    sha256_hex  NOT NULL UNIQUE,   -- SHA-256 of the key; the key is shown once and never stored
    created_at  timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, key_id)
);

COMMENT ON TABLE accountant_api_keys IS
    'Which tenant an accountant API key opens (§28). Found by the fingerprint presented (app.api_key_hash).';

CALL enable_tenant_isolation('accountant_api_keys');
CREATE POLICY presented_key ON accountant_api_keys FOR SELECT
    USING (key_hash = current_setting('app.api_key_hash', true));

GRANT SELECT, INSERT, DELETE ON accountant_api_keys TO backoffice_app;

-- --------------------------------------------------------------------- stored sign-ins (0006)

-- The api seals and opens connection secrets itself (connectors/vault.py).
GRANT SELECT, INSERT, UPDATE, DELETE ON connection_credentials TO backoffice_app;

-- --------------------------------------------------------------------- mailbox sign-ins in progress

CREATE TABLE oauth_nonces (
    tenant_id   tenant_key  NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    nonce       text        NOT NULL CHECK (nonce ~ '^[A-Za-z0-9_-]{16,64}$'),
    expires_at  timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, nonce)
);

COMMENT ON TABLE oauth_nonces IS
    'Google/Microsoft sign-ins started and not finished (10 minutes). Each nonce can finish once.';

CALL enable_tenant_isolation('oauth_nonces');

GRANT SELECT, INSERT, DELETE ON oauth_nonces TO backoffice_app;
