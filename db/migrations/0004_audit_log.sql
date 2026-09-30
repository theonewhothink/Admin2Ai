-- 0004_audit_log: append-only, hash-chained audit history (§52 immutable
-- audit history, §55 provenance).
--
-- Layout is the one backoffice.audit.PostgresAuditStore reads and writes; use
-- it with table="audit_log". Each tenant has its own chain:
--
--     hash = SHA-256(prev_hash || '\n' || body)   (HMAC-SHA-256 when keyed)
--
-- and the first record links to the tenant's genesis hash
-- SHA-256('backoffice.audit.v1:' || tenant_id). body is TEXT, not jsonb: the
-- hashed bytes must come back exactly as written. The database cannot recompute
-- a keyed hash (the key never reaches it), but it does enforce that every new
-- record extends the current head: next seq, prev_hash = head hash, time never
-- going backwards. UPDATE, DELETE and TRUNCATE are refused.

CREATE TABLE audit_log (
    tenant_id   tenant_key  NOT NULL REFERENCES tenants (id),
    seq         bigint      NOT NULL CHECK (seq >= 1),
    at          timestamptz NOT NULL,
    body        text        NOT NULL,
    prev_hash   sha256_hex  NOT NULL,
    hash        sha256_hex  NOT NULL,
    -- Read-only projections of the body for access-log queries (§52).
    actor       text        GENERATED ALWAYS AS ((body::jsonb) ->> 'actor') STORED,
    action      text        GENERATED ALWAYS AS ((body::jsonb) ->> 'action') STORED,
    subject_id  text        GENERATED ALWAYS AS ((body::jsonb) ->> 'subject_id') STORED,
    PRIMARY KEY (tenant_id, seq),
    UNIQUE (tenant_id, prev_hash),
    UNIQUE (hash),
    CHECK (hash <> prev_hash)
);

COMMENT ON TABLE audit_log IS
    'Hash-chained audit records (§52, §55). PostgresAuditStore(table="audit_log"). Append-only.';

CREATE INDEX audit_log_subject_idx ON audit_log (tenant_id, subject_id, seq) WHERE subject_id IS NOT NULL;
CREATE INDEX audit_log_actor_idx ON audit_log (tenant_id, actor, at);
CREATE INDEX audit_log_at_idx ON audit_log (tenant_id, at);

CREATE FUNCTION audit_genesis_hash(p_tenant text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN encode(sha256(convert_to('backoffice.audit.v1:' || p_tenant, 'UTF8')), 'hex');

COMMENT ON FUNCTION audit_genesis_hash(text) IS 'prev_hash of a tenant''s first audit record.';

CREATE FUNCTION check_audit_chain() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public
AS $$
DECLARE
    head audit_log%ROWTYPE;
BEGIN
    SELECT * INTO head FROM audit_log
    WHERE tenant_id = NEW.tenant_id
    ORDER BY seq DESC
    LIMIT 1;
    IF NOT FOUND THEN
        IF NEW.seq <> 1 OR NEW.prev_hash::text <> audit_genesis_hash(NEW.tenant_id) THEN
            RAISE EXCEPTION 'the first audit record of a tenant must have seq 1 and the genesis hash'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.seq <> head.seq + 1 OR NEW.prev_hash <> head.hash THEN
        RAISE EXCEPTION 'audit record % does not extend the head %', NEW.seq, head.seq
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.at < head.at THEN
        RAISE EXCEPTION 'audit record % is dated before the head', NEW.seq
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER audit_log_chain BEFORE INSERT ON audit_log
    FOR EACH ROW EXECUTE FUNCTION check_audit_chain();
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

CALL enable_tenant_isolation('audit_log');

GRANT SELECT, INSERT ON audit_log TO backoffice_app, backoffice_evidence_admin;
GRANT SELECT ON audit_log TO backoffice_readonly;
