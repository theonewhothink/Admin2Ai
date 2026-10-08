-- 0016_jobs_and_webhooks: the durable work queue, and what mail providers' push notifications need (§45, §47).
--
-- jobs              Work one process asks another to do, kept in PostgreSQL next to the event log
--                   (the API receives a push notification; the sync worker reads the mailbox). A job is
--                   claimed with SELECT ... FOR UPDATE SKIP LOCKED, retried with backoff, and after its
--                   last attempt parked as 'dead' (a dead letter) for engineers: never dropped. Only one
--                   queued job per (business, kind, key): a burst of notifications is one sync. Payloads
--                   hold ids only, never a secret or a document.
-- webhook_routes    Which business and connection a push notification is for: Gmail names the mailbox,
--                   Microsoft Graph its subscription id. A notification carries no business scope, so
--                   the API presents what the notification names (app.webhook_route =
--                   '<provider>:<key>') and sees only matching rows. secret_hash is the SHA-256 of a
--                   Graph subscription's clientState (the secret itself is never stored).
-- webhook_receipts  Notifications already handled, by the provider's id (or a hash of the notification),
--                   kept until expires_at: a duplicate or replayed notification does nothing.
--
-- Row-level security: tenant isolation on all three (app.tenant_id). The sync worker claims, retries
-- and buries jobs of every business as backoffice_scheduler (only after SET ROLE; its login holds the
-- membership WITH INHERIT FALSE), which is the only role that sees jobs across businesses. Erasing a
-- business removes its rows (ON DELETE CASCADE).

CREATE TABLE jobs (
    id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     tenant_key  NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    kind          text        NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_.]{1,63}$'),
    dedupe_key    text        NOT NULL CHECK (length(dedupe_key) BETWEEN 1 AND 200),
    payload       jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
    state         text        NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'done', 'dead')),
    attempts      integer     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts  integer     NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 50),
    run_after     timestamptz NOT NULL,
    locked_until  timestamptz,
    last_error    text        CHECK (last_error IS NULL OR length(last_error) <= 200),
    created_at    timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL,
    finished_at   timestamptz
);

COMMENT ON TABLE jobs IS
    'Durable work queue (§45): claimed with FOR UPDATE SKIP LOCKED, retried with backoff, parked as dead after max_attempts.';

CREATE UNIQUE INDEX jobs_queued_once ON jobs (tenant_id, kind, dedupe_key) WHERE state = 'queued';
CREATE INDEX jobs_due_idx ON jobs (run_after, id) WHERE state IN ('queued', 'running');
CREATE INDEX jobs_dead_idx ON jobs (updated_at) WHERE state = 'dead';
CREATE INDEX jobs_tenant_idx ON jobs (tenant_id);

CALL enable_tenant_isolation('jobs');
CREATE POLICY scheduler_runs_jobs ON jobs TO backoffice_scheduler USING (true) WITH CHECK (true);

GRANT SELECT, INSERT ON jobs TO backoffice_app;
GRANT SELECT, DELETE ON jobs TO backoffice_scheduler;
GRANT UPDATE (state, attempts, run_after, locked_until, last_error, updated_at, finished_at) ON jobs
    TO backoffice_scheduler;
GRANT SELECT ON jobs TO backoffice_readonly;

CREATE TABLE webhook_routes (
    provider          text        NOT NULL CHECK (provider IN ('gmail', 'microsoft')),
    route_key         text        NOT NULL CHECK (length(route_key) BETWEEN 1 AND 320),
    tenant_id         tenant_key  NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    connection_id     bo_id       NOT NULL,
    secret_hash       text        CHECK (secret_hash IS NULL OR secret_hash ~ '^[0-9a-f]{64}$'),
    expires_at        timestamptz,
    last_notified_at  timestamptz,
    created_at        timestamptz NOT NULL,
    updated_at        timestamptz NOT NULL,
    PRIMARY KEY (provider, route_key, tenant_id, connection_id)
);

COMMENT ON TABLE webhook_routes IS
    'Which business and connection a push notification names (§47). Found by what the notification presents (app.webhook_route).';

CREATE INDEX webhook_routes_connection_idx ON webhook_routes (tenant_id, connection_id);

CALL enable_tenant_isolation('webhook_routes');
CREATE POLICY presented_route ON webhook_routes FOR SELECT
    USING (provider || ':' || route_key = current_setting('app.webhook_route', true));

GRANT SELECT, INSERT, UPDATE, DELETE ON webhook_routes TO backoffice_app;
GRANT SELECT ON webhook_routes TO backoffice_readonly;

CREATE TABLE webhook_receipts (
    tenant_id        tenant_key  NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    provider         text        NOT NULL CHECK (provider IN ('gmail', 'microsoft')),
    notification_id  text        NOT NULL CHECK (length(notification_id) BETWEEN 1 AND 200),
    received_at      timestamptz NOT NULL,
    expires_at       timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, provider, notification_id)
);

COMMENT ON TABLE webhook_receipts IS
    'Push notifications already handled, kept until expires_at: duplicates and replays are harmless (§47).';

CREATE INDEX webhook_receipts_expiry_idx ON webhook_receipts (tenant_id, expires_at);

CALL enable_tenant_isolation('webhook_receipts');

GRANT SELECT, INSERT, DELETE ON webhook_receipts TO backoffice_app;
