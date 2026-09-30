-- 0001_core: foundations, tenancy, identity, entities, suppliers, connectors,
-- immutable evidence and the evidence-deletion workflow.
--
-- Mirrors backend/src/backoffice/domain/models.py (§7: the model starts from
-- EVIDENCE, not from Invoice). Conventions used by every migration:
--
--   * Every tenant-owned table has "tenant_id tenant_key NOT NULL", a primary
--     key that starts with tenant_id, and Row-Level Security enabled AND forced
--     through enable_tenant_isolation(): rows are visible and writable only when
--     tenant_id = current_setting('app.tenant_id', true) (§52 tenant isolation).
--     With the setting absent every query sees nothing (fail closed).
--   * References between tenant tables are composite (tenant_id, id) foreign
--     keys, so a row can never point at another tenant's row.
--   * Money is numeric(18,2) next to a currency char(3) (ISO 4217). Never float.
--     numeric(18,2) ROUNDS extra decimals on input: callers must pass amounts
--     already exact to the cent (backoffice_db.db_amount refuses anything else).
--   * Timestamps are timestamptz. Enumerations mirror the Python enums by value.
--   * Append-only tables refuse UPDATE/DELETE/TRUNCATE with a trigger (§52
--     immutable history, §55 "original never overwritten").
--
-- The runner (python -m backoffice_db migrate) wraps each file in one
-- transaction; files therefore contain no BEGIN/COMMIT and no psql commands.

-- --------------------------------------------------------------------- hardening

REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- --------------------------------------------------------------------- roles
-- NOLOGIN group roles. Login users (one per service) are created outside the
-- migrations, with their passwords, and granted one of these roles:
--   backoffice_app            api + worker: DML under RLS, no DELETE on history
--   backoffice_readonly       support/analytics: SELECT under RLS
--   backoffice_evidence_admin the hard-approved evidence deletion workflow (§25)
--   backoffice_scheduler      may list tenant ids to fan out periodic work
-- None of them may bypass RLS or own tables.

DO $roles$
DECLARE
    r text;
BEGIN
    FOREACH r IN ARRAY ARRAY[
        'backoffice_app', 'backoffice_readonly',
        'backoffice_evidence_admin', 'backoffice_scheduler'
    ] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            BEGIN
                EXECUTE format('CREATE ROLE %I NOLOGIN NOBYPASSRLS', r);
            EXCEPTION WHEN duplicate_object THEN
                NULL;  -- created concurrently by another database's migration
            END;
        END IF;
    END LOOP;
END
$roles$;

GRANT USAGE ON SCHEMA public TO backoffice_app, backoffice_readonly,
    backoffice_evidence_admin, backoffice_scheduler;

-- --------------------------------------------------------------------- domains

-- Tenant ids double as the first segment of evidence object keys
-- ("<tenant>/sha256/<xx>/<sha256>"), so they use the object store's alphabet.
CREATE DOMAIN tenant_key AS text
    CHECK (VALUE ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$');

-- Ids made by models.new_id ("ev_1a2b...") and by external systems.
CREATE DOMAIN bo_id AS text
    CHECK (VALUE ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$');

CREATE DOMAIN nonblank_text AS text
    CHECK (VALUE ~ '\S');

-- Lower-case identifier for open vocabularies (field names, tags, factors).
CREATE DOMAIN slug AS text
    CHECK (VALUE ~ '^[a-z][a-z0-9_]{0,62}$');

CREATE DOMAIN currency_code AS char(3)
    CHECK (VALUE ~ '^[A-Z]{3}$');

CREATE DOMAIN country_code AS char(2)
    CHECK (VALUE ~ '^[A-Z]{2}$');

CREATE DOMAIN sha256_hex AS char(64)
    CHECK (VALUE ~ '^[0-9a-f]{64}$');

-- Normalised IBAN: upper case, no spaces, 15-34 characters. Check digits are
-- validated in the application (fraud/verification), not here.
CREATE DOMAIN iban_code AS text
    CHECK (VALUE ~ '^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$');

CREATE DOMAIN email_domain AS text
    CHECK (VALUE ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$');

-- Enumerations: values mirror backoffice.domain (tests compare them).
CREATE DOMAIN source_kind AS text CHECK (VALUE IN (
    'email', 'bank', 'card', 'supplier_portal', 'accounting_system',
    'cloud_storage', 'upload', 'mobile_scan', 'mobile_share', 'government',
    'accountant'
));

CREATE DOMAIN evidence_format AS text CHECK (VALUE IN (
    'pdf', 'image', 'screenshot', 'qr', 'email', 'eml', 'html', 'url', 'xml',
    'ubl', 'saft', 'json', 'csv', 'xlsx', 'zip', 'bank_transaction',
    'card_transaction', 'government_notice', 'text'
));

-- Quality: GREEN = 'verified', AMBER = 'likely', RED = 'conflict' (§57).
CREATE DOMAIN quality_level AS text CHECK (VALUE IN ('verified', 'likely', 'conflict'));

CREATE DOMAIN membership_role AS text CHECK (VALUE IN ('owner', 'accountant'));

-- Mirrors backoffice.connectors.base.ConnectorKind / WebhookState (§47).
CREATE DOMAIN connector_kind AS text CHECK (VALUE IN (
    'gmail', 'microsoft', 'imap', 'open_banking', 'supplier_portal'
));

CREATE DOMAIN webhook_state AS text CHECK (VALUE IN ('not_used', 'active', 'expired', 'failed'));

-- --------------------------------------------------------------------- helpers

CREATE FUNCTION all_nonblank(values_ text[]) RETURNS boolean
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN NOT EXISTS (SELECT 1 FROM unnest(values_) AS v WHERE v IS NULL OR v !~ '\S');

COMMENT ON FUNCTION all_nonblank(text[]) IS
    'True when no element is NULL or blank (evidence id lists, reasons).';

CREATE PROCEDURE enable_tenant_isolation(tbl regclass)
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public
AS $$
BEGIN
    EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', tbl);
    EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', tbl);
    EXECUTE format(
        'CREATE POLICY tenant_isolation ON %s '
        'USING (tenant_id = current_setting(''app.tenant_id'', true)) '
        'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))',
        tbl
    );
END
$$;

COMMENT ON PROCEDURE enable_tenant_isolation(regclass) IS
    'Migration helper: ENABLE + FORCE row level security with the app.tenant_id policy (§52).';

CREATE FUNCTION forbid_mutation() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation',
              HINT = 'Record a correction as a new row.';
END
$$;

CREATE FUNCTION touch_updated_at() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END
$$;

REVOKE EXECUTE ON PROCEDURE enable_tenant_isolation(regclass) FROM PUBLIC;

-- --------------------------------------------------------------------- tenancy

CREATE TABLE tenants (
    id          tenant_key    PRIMARY KEY,
    name        nonblank_text NOT NULL,
    country     country_code  NOT NULL,
    created_at  timestamptz   NOT NULL DEFAULT now()
);

COMMENT ON TABLE tenants IS 'One customer account (§51: one human, many companies, one account).';

ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenants
    USING (id = current_setting('app.tenant_id', true))
    WITH CHECK (id = current_setting('app.tenant_id', true));
-- Fan-out of periodic work (month-end autopilot) needs the list of tenant ids;
-- everything else is still read per tenant under app.tenant_id.
CREATE POLICY scheduler_lists_tenants ON tenants FOR SELECT TO backoffice_scheduler
    USING (true);

CREATE TABLE users (
    id                bo_id         PRIMARY KEY,
    external_subject  nonblank_text UNIQUE,          -- identity provider subject ("sub")
    email             nonblank_text NOT NULL UNIQUE CHECK (email = lower(email)),
    display_name      text,
    locale            text,
    created_at        timestamptz   NOT NULL DEFAULT now()
);

COMMENT ON TABLE users IS
    'People who sign in. Global: one person may own a business and be accountant for others (§28).';

CREATE TABLE memberships (
    tenant_id   tenant_key      NOT NULL REFERENCES tenants (id),
    user_id     bo_id           NOT NULL REFERENCES users (id),
    role        membership_role NOT NULL,
    invited_by  bo_id           REFERENCES users (id),
    created_at  timestamptz     NOT NULL DEFAULT now(),
    revoked_at  timestamptz,
    PRIMARY KEY (tenant_id, user_id, role),
    CHECK (revoked_at IS NULL OR revoked_at >= created_at)
);

CREATE INDEX memberships_user_idx ON memberships (user_id) WHERE revoked_at IS NULL;

CALL enable_tenant_isolation('memberships');
-- A signed-in person may list their own memberships to choose a tenant.
CREATE POLICY own_memberships ON memberships FOR SELECT
    USING (user_id = current_setting('app.user_id', true));

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
CREATE POLICY self_or_same_tenant ON users FOR SELECT
    USING (
        id = current_setting('app.user_id', true)
        OR EXISTS (
            SELECT 1 FROM memberships m
            WHERE m.user_id = users.id
              AND m.tenant_id = current_setting('app.tenant_id', true)
        )
    );
CREATE POLICY self_write ON users FOR INSERT
    WITH CHECK (id = current_setting('app.user_id', true));
CREATE POLICY self_update ON users FOR UPDATE
    USING (id = current_setting('app.user_id', true))
    WITH CHECK (id = current_setting('app.user_id', true));

-- --------------------------------------------------------------------- legal entities

CREATE TABLE legal_entities (
    tenant_id   tenant_key    NOT NULL REFERENCES tenants (id),
    id          bo_id         NOT NULL,
    name        nonblank_text NOT NULL,
    country     country_code  NOT NULL,
    tax_id      nonblank_text NOT NULL,
    created_at  timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, country, tax_id)
);

COMMENT ON TABLE legal_entities IS 'Companies of one tenant (§51 multi-entity).';

CREATE TABLE legal_entity_ibans (
    tenant_id   tenant_key NOT NULL,
    entity_id   bo_id      NOT NULL,
    iban        iban_code  NOT NULL,
    added_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, entity_id, iban),
    FOREIGN KEY (tenant_id, entity_id) REFERENCES legal_entities (tenant_id, id) ON DELETE CASCADE
);

-- A company's own IBAN identifies internal transfers (§21): one owner per IBAN.
CREATE UNIQUE INDEX legal_entity_ibans_iban_key ON legal_entity_ibans (tenant_id, iban);

-- --------------------------------------------------------------------- suppliers

CREATE TABLE suppliers (
    tenant_id      tenant_key     NOT NULL REFERENCES tenants (id),
    id             bo_id          NOT NULL,
    name           nonblank_text  NOT NULL,
    tax_id         nonblank_text,
    countries      country_code[] NOT NULL DEFAULT '{}',
    contact_email  nonblank_text,
    created_at     timestamptz    NOT NULL DEFAULT now(),
    updated_at     timestamptz    NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);

CREATE INDEX suppliers_tax_id_idx ON suppliers (tenant_id, tax_id) WHERE tax_id IS NOT NULL;
CREATE INDEX suppliers_name_idx ON suppliers (tenant_id, lower(name));

CREATE TRIGGER suppliers_touch BEFORE UPDATE ON suppliers
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE supplier_aliases (
    tenant_id    tenant_key    NOT NULL,
    supplier_id  bo_id         NOT NULL,
    alias        nonblank_text NOT NULL,
    PRIMARY KEY (tenant_id, supplier_id, alias),
    FOREIGN KEY (tenant_id, supplier_id) REFERENCES suppliers (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX supplier_aliases_lookup_idx ON supplier_aliases (tenant_id, lower(alias));

-- Known beneficiary accounts. A new or changed supplier IBAN is a fraud
-- hard-stop and a hard approval (§25, §26): rows are never edited or deleted,
-- only revoked, and each records who added it and on what approval.
CREATE TABLE supplier_known_ibans (
    tenant_id           tenant_key    NOT NULL,
    supplier_id         bo_id         NOT NULL,
    iban                iban_code     NOT NULL,
    added_at            timestamptz   NOT NULL DEFAULT now(),
    added_by            nonblank_text NOT NULL,
    approval_reference  nonblank_text,   -- hard-approval record; NULL only for IBANs learned from paid history
    revoked_at          timestamptz,
    revoked_by          nonblank_text,
    PRIMARY KEY (tenant_id, supplier_id, iban),
    FOREIGN KEY (tenant_id, supplier_id) REFERENCES suppliers (tenant_id, id),
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL))
);

CREATE INDEX supplier_known_ibans_iban_idx ON supplier_known_ibans (tenant_id, iban);

CREATE FUNCTION guard_supplier_known_iban() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'supplier_known_ibans rows are revoked, never deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.revoked_at IS NOT NULL
       OR (NEW.tenant_id, NEW.supplier_id, NEW.iban, NEW.added_at, NEW.added_by, NEW.approval_reference)
          IS DISTINCT FROM
          (OLD.tenant_id, OLD.supplier_id, OLD.iban, OLD.added_at, OLD.added_by, OLD.approval_reference)
    THEN
        RAISE EXCEPTION 'a known IBAN can only be revoked, once'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER supplier_known_ibans_guard BEFORE UPDATE OR DELETE ON supplier_known_ibans
    FOR EACH ROW EXECUTE FUNCTION guard_supplier_known_iban();

CREATE TABLE supplier_email_domains (
    tenant_id    tenant_key   NOT NULL,
    supplier_id  bo_id        NOT NULL,
    domain       email_domain NOT NULL,
    added_at     timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, supplier_id, domain),
    FOREIGN KEY (tenant_id, supplier_id) REFERENCES suppliers (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX supplier_email_domains_domain_idx ON supplier_email_domains (tenant_id, domain);

-- --------------------------------------------------------------------- sources / connectors

-- One row per connection, mirroring backoffice.connectors.base.ConnectorState
-- (§47): last successful sync, last event, cursor, auth expiry, webhook state,
-- historical coverage and known gaps, failures. Credentials never live here:
-- credentials_ref points into the secrets vault (§52).
CREATE TABLE connectors (
    tenant_id             tenant_key      NOT NULL REFERENCES tenants (id),
    id                    bo_id           NOT NULL,
    kind                  connector_kind  NOT NULL,
    source_kind           source_kind     NOT NULL,
    account               nonblank_text   NOT NULL,  -- mailbox, bank account label or portal login
    display_name          text,
    entity_id             bo_id,
    credentials_ref       text,                       -- secrets-vault reference, never a secret
    last_successful_sync  timestamptz,
    last_attempt_at       timestamptz,
    last_event_at         timestamptz,
    cursor                text,
    auth_expires_at       timestamptz,
    reconnect_required    boolean         NOT NULL DEFAULT false,
    webhook_state         webhook_state   NOT NULL DEFAULT 'not_used',
    webhook_expires_at    timestamptz,
    coverage_start        timestamptz,
    coverage_end          timestamptz,
    known_gaps            tstzmultirange  NOT NULL DEFAULT '{}',
    consecutive_failures  integer         NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    last_error_code       text,                       -- internal only, never owner copy (§48)
    version               integer         NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at            timestamptz     NOT NULL DEFAULT now(),
    updated_at            timestamptz     NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, kind, account),
    FOREIGN KEY (tenant_id, entity_id) REFERENCES legal_entities (tenant_id, id),
    CHECK ((coverage_start IS NULL) = (coverage_end IS NULL)),
    CHECK (coverage_start IS NULL OR coverage_start < coverage_end),
    CHECK (credentials_ref IS NULL OR credentials_ref ~ '\S')
);

COMMENT ON TABLE connectors IS
    'Sources (§8, §47). A connector that stopped syncing means the month can never be green.';
COMMENT ON COLUMN connectors.version IS 'Optimistic concurrency: UPDATE ... WHERE version = $expected.';

CREATE TRIGGER connectors_touch BEFORE UPDATE ON connectors
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Bank and card accounts that transactions are booked on.
CREATE TABLE accounts (
    tenant_id     tenant_key    NOT NULL REFERENCES tenants (id),
    id            bo_id         NOT NULL,
    kind          text          NOT NULL CHECK (kind IN ('bank', 'card')),
    name          nonblank_text NOT NULL,
    entity_id     bo_id,
    connector_id  bo_id,
    iban          iban_code,
    card_last4    char(4)       CHECK (card_last4 ~ '^[0-9]{4}$'),
    currency      currency_code NOT NULL DEFAULT 'EUR',
    created_at    timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, entity_id) REFERENCES legal_entities (tenant_id, id),
    FOREIGN KEY (tenant_id, connector_id) REFERENCES connectors (tenant_id, id)
);

-- --------------------------------------------------------------------- evidence

-- The immutable original (§7, §55). Bytes live in the object store under
-- storage_key; this row is never updated. Deletion is only possible through
-- an approved evidence_deletion_requests row (§25 hard approval, §52 deletion
-- workflows), executed by execute_evidence_deletion().
CREATE TABLE evidence (
    tenant_id     tenant_key      NOT NULL REFERENCES tenants (id),
    id            bo_id           NOT NULL,
    source_kind   source_kind     NOT NULL,
    format        evidence_format NOT NULL,
    sha256        sha256_hex      NOT NULL,
    size_bytes    bigint          CHECK (size_bytes >= 0),
    storage_key   text            CHECK (storage_key IS NULL OR right(storage_key, 64) = sha256::text),
    original_url  text,
    retrieved_at  timestamptz     NOT NULL,
    filename      text,
    mime_type     text,
    connector_id  bo_id,
    metadata      jsonb           NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(metadata) = 'object'),
    created_at    timestamptz     NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, sha256),
    FOREIGN KEY (tenant_id, connector_id) REFERENCES connectors (tenant_id, id)
);

COMMENT ON TABLE evidence IS
    'Immutable originals (§7, §55). UPDATE is always refused; DELETE only via execute_evidence_deletion().';
COMMENT ON COLUMN evidence.storage_key IS 'Object key; always ends with the sha256 of the bytes.';

CREATE INDEX evidence_retrieved_idx ON evidence (tenant_id, retrieved_at);

-- Every time the same bytes are seen again (another email, a portal, a scan).
CREATE TABLE evidence_sightings (
    tenant_id     tenant_key  NOT NULL,
    id            bigint      GENERATED ALWAYS AS IDENTITY,
    evidence_id   bo_id       NOT NULL,
    source_kind   source_kind NOT NULL,
    seen_at       timestamptz NOT NULL,
    original_url  text,
    filename      text,
    context       jsonb       NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(context) = 'object'),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, evidence_id) REFERENCES evidence (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX evidence_sightings_evidence_idx ON evidence_sightings (tenant_id, evidence_id, seen_at);

-- --------------------------------------------------------------------- evidence deletion workflow

-- The only way to remove an original. Rows are permanent (they are the record
-- of what was deleted, when, why and on whose approval). Execution is done by
-- execute_evidence_deletion() (0002, once the derived tables exist):
--   requested -> approved | rejected | cancelled
--   approved  -> executed | cancelled
--   executed  -> (object_purged_at set once, after the object store purge)
CREATE TABLE evidence_deletion_requests (
    tenant_id           tenant_key    NOT NULL REFERENCES tenants (id),
    id                  bo_id         NOT NULL,
    evidence_id         bo_id         NOT NULL,  -- no FK: the evidence row is gone once executed
    evidence_sha256     sha256_hex    NOT NULL,
    storage_key         text,
    reason              nonblank_text NOT NULL,
    requested_by        nonblank_text NOT NULL,
    requested_at        timestamptz   NOT NULL DEFAULT now(),
    status              text          NOT NULL DEFAULT 'requested'
                        CHECK (status IN ('requested', 'approved', 'rejected', 'cancelled', 'executed')),
    approval_reference  nonblank_text,           -- the hard-approval record (§25)
    approved_by         bo_id,                   -- an owner of the tenant
    approved_at         timestamptz,
    decided_at          timestamptz,
    executed_by         nonblank_text,
    executed_at         timestamptz,
    object_purged_at    timestamptz,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (approved_by) REFERENCES users (id),
    CHECK (status NOT IN ('approved', 'executed')
           OR (approval_reference IS NOT NULL AND approved_by IS NOT NULL AND approved_at IS NOT NULL)),
    CHECK (status <> 'executed' OR (executed_at IS NOT NULL AND executed_by IS NOT NULL)),
    CHECK (object_purged_at IS NULL OR status = 'executed')
);

COMMENT ON TABLE evidence_deletion_requests IS
    'Deletion of original evidence always needs hard approval (§25) and leaves this permanent record.';

CREATE UNIQUE INDEX evidence_deletion_one_open_idx ON evidence_deletion_requests (tenant_id, evidence_id)
    WHERE status IN ('requested', 'approved');

-- True inside execute_evidence_deletion() for the evidence it is deleting.
CREATE FUNCTION evidence_deletion_allowed(p_tenant text, p_evidence text) RETURNS boolean
    LANGUAGE sql STABLE
    SET search_path = pg_catalog, public
    RETURN p_evidence IS NOT NULL AND EXISTS (
        SELECT 1 FROM evidence_deletion_requests r
        WHERE r.id = nullif(current_setting('backoffice.evidence_deletion_request', true), '')
          AND r.tenant_id = p_tenant
          AND r.evidence_id = p_evidence
          AND r.status = 'approved'
    );

CREATE FUNCTION guard_evidence() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' AND evidence_deletion_allowed(OLD.tenant_id, OLD.id) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'evidence is immutable: % is not allowed', TG_OP
        USING ERRCODE = 'restrict_violation',
              HINT = 'Originals change only through an approved evidence deletion request.';
END
$$;

CREATE TRIGGER evidence_immutable BEFORE UPDATE OR DELETE ON evidence
    FOR EACH ROW EXECUTE FUNCTION guard_evidence();
CREATE TRIGGER evidence_no_truncate BEFORE TRUNCATE ON evidence
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- Append-only rows that belong to one piece of evidence: they disappear only
-- together with it, inside an approved deletion.
CREATE FUNCTION forbid_mutation_except_evidence_deletion() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' AND evidence_deletion_allowed(OLD.tenant_id, OLD.evidence_id) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END
$$;

CREATE TRIGGER evidence_sightings_append_only BEFORE UPDATE OR DELETE ON evidence_sightings
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation_except_evidence_deletion();
CREATE TRIGGER evidence_sightings_no_truncate BEFORE TRUNCATE ON evidence_sightings
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

CREATE FUNCTION guard_evidence_deletion_request() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'requested' THEN
            RAISE EXCEPTION 'a deletion request starts as requested' USING ERRCODE = 'check_violation';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM evidence e
            WHERE e.tenant_id = NEW.tenant_id AND e.id = NEW.evidence_id AND e.sha256 = NEW.evidence_sha256
        ) THEN
            RAISE EXCEPTION 'no such evidence with that sha256' USING ERRCODE = 'foreign_key_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'evidence deletion requests are permanent' USING ERRCODE = 'restrict_violation';
    END IF;

    IF (NEW.tenant_id, NEW.id, NEW.evidence_id, NEW.evidence_sha256, NEW.storage_key,
        NEW.reason, NEW.requested_by, NEW.requested_at)
       IS DISTINCT FROM
       (OLD.tenant_id, OLD.id, OLD.evidence_id, OLD.evidence_sha256, OLD.storage_key,
        OLD.reason, OLD.requested_by, OLD.requested_at)
    THEN
        RAISE EXCEPTION 'what was requested cannot change' USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = NEW.status THEN
        IF NEW.status = 'executed' AND OLD.object_purged_at IS NULL
           AND NEW.object_purged_at IS NOT NULL
           AND (NEW.approval_reference, NEW.approved_by, NEW.approved_at, NEW.decided_at,
                NEW.executed_by, NEW.executed_at)
               IS NOT DISTINCT FROM
               (OLD.approval_reference, OLD.approved_by, OLD.approved_at, OLD.decided_at,
                OLD.executed_by, OLD.executed_at)
        THEN
            RETURN NEW;  -- the object store copy has been purged
        END IF;
        RAISE EXCEPTION 'deletion request % is already %', OLD.id, OLD.status
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NOT (
        (OLD.status = 'requested' AND NEW.status IN ('approved', 'rejected', 'cancelled'))
        OR (OLD.status = 'approved' AND NEW.status IN ('executed', 'cancelled'))
    ) THEN
        RAISE EXCEPTION 'deletion request cannot go from % to %', OLD.status, NEW.status
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW.status = 'approved'
       AND (NEW.approval_reference IS NULL OR NEW.approved_by IS NULL OR NEW.approved_at IS NULL) THEN
        RAISE EXCEPTION 'an approval needs its reference, its approver and its time'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.status = 'approved' AND NOT EXISTS (
        SELECT 1 FROM memberships m
        WHERE m.tenant_id = NEW.tenant_id AND m.user_id = NEW.approved_by
          AND m.role = 'owner' AND m.revoked_at IS NULL
    ) THEN
        RAISE EXCEPTION 'deleting evidence needs the approval of an owner of this business'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    IF NEW.status = 'executed' AND EXISTS (
        SELECT 1 FROM evidence e WHERE e.tenant_id = NEW.tenant_id AND e.id = NEW.evidence_id
    ) THEN
        RAISE EXCEPTION 'use execute_evidence_deletion() to execute a deletion'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW.status <> 'approved' AND NEW.status <> 'executed' THEN
        NEW.decided_at := coalesce(NEW.decided_at, now());
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER evidence_deletion_requests_guard
    BEFORE INSERT OR UPDATE OR DELETE ON evidence_deletion_requests
    FOR EACH ROW EXECUTE FUNCTION guard_evidence_deletion_request();
CREATE TRIGGER evidence_deletion_requests_no_truncate BEFORE TRUNCATE ON evidence_deletion_requests
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

CALL enable_tenant_isolation('legal_entities');
CALL enable_tenant_isolation('legal_entity_ibans');
CALL enable_tenant_isolation('suppliers');
CALL enable_tenant_isolation('supplier_aliases');
CALL enable_tenant_isolation('supplier_known_ibans');
CALL enable_tenant_isolation('supplier_email_domains');
CALL enable_tenant_isolation('connectors');
CALL enable_tenant_isolation('accounts');
CALL enable_tenant_isolation('evidence');
CALL enable_tenant_isolation('evidence_sightings');
CALL enable_tenant_isolation('evidence_deletion_requests');

-- --------------------------------------------------------------------- grants

GRANT SELECT, INSERT ON tenants, users, memberships TO backoffice_app;
GRANT UPDATE (name) ON tenants TO backoffice_app;
GRANT UPDATE (display_name, locale, email) ON users TO backoffice_app;
GRANT UPDATE (revoked_at) ON memberships TO backoffice_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON legal_entities, legal_entity_ibans, suppliers,
    supplier_aliases, supplier_email_domains, connectors, accounts TO backoffice_app;
GRANT SELECT, INSERT ON supplier_known_ibans TO backoffice_app;
GRANT UPDATE (revoked_at, revoked_by) ON supplier_known_ibans TO backoffice_app;
GRANT SELECT, INSERT ON evidence, evidence_sightings, evidence_deletion_requests TO backoffice_app;

GRANT SELECT ON tenants, users, memberships, legal_entities, legal_entity_ibans, suppliers,
    supplier_aliases, supplier_known_ibans, supplier_email_domains, connectors, accounts,
    evidence, evidence_sightings, evidence_deletion_requests TO backoffice_readonly;

GRANT SELECT ON tenants TO backoffice_scheduler;

GRANT SELECT ON evidence, evidence_deletion_requests, memberships TO backoffice_evidence_admin;
GRANT UPDATE (status, approval_reference, approved_by, approved_at, decided_at, object_purged_at)
    ON evidence_deletion_requests TO backoffice_evidence_admin;
