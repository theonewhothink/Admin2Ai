-- 0002_documents_transactions: documents, field-level observations and
-- verification (§18), bank/card transactions, matches with their "Why?" lines
-- (§20, §54), and the executor of approved evidence deletions (§25, §52).

-- --------------------------------------------------------------------- enumerations

CREATE DOMAIN document_type AS text CHECK (VALUE IN (
    'invoice', 'invoice_receipt', 'simplified_invoice', 'receipt', 'credit_note',
    'debit_note', 'statement', 'tax_notice', 'payroll', 'loan_statement',
    'contract', 'other'
));

CREATE DOMAIN extraction_method AS text CHECK (VALUE IN (
    'structured_xml', 'embedded_text', 'qr', 'barcode', 'api', 'html_structured',
    'ocr', 'vlm', 'arithmetic', 'bank', 'human'
));

CREATE DOMAIN transaction_kind AS text CHECK (VALUE IN (
    'card', 'transfer_out', 'transfer_in', 'direct_debit', 'fee', 'internal'
));

-- Mirrors backoffice.reconciliation.engine.MatchKind.
CREATE DOMAIN match_kind AS text CHECK (VALUE IN (
    'one_to_one', 'one_payment_many_documents', 'many_payments_one_document',
    'partial_payment', 'card_settlement'
));

-- --------------------------------------------------------------------- documents

-- The understood document (models.Document). Derived data: it may be
-- re-extracted and updated; the originals stay in evidence.
CREATE TABLE documents (
    tenant_id          tenant_key    NOT NULL REFERENCES tenants (id),
    id                 bo_id         NOT NULL,
    doc_type           document_type NOT NULL DEFAULT 'other',
    supplier_id        bo_id,
    supplier_name      text,
    supplier_tax_id    text,
    customer_tax_id    text,
    invoice_number     text,
    issue_date         date,
    due_date           date,
    currency           currency_code NOT NULL DEFAULT 'EUR',
    net_amount         numeric(18,2),
    vat_amount         numeric(18,2),
    gross_amount       numeric(18,2),   -- credit notes are stored positive; doc_type gives the sign
    iban               iban_code,
    payment_reference  text,
    entity_id          bo_id,
    quality            quality_level NOT NULL DEFAULT 'likely',
    duplicate_of       bo_id,           -- merged duplicate -> surviving document (§25)
    created_at         timestamptz   NOT NULL DEFAULT now(),
    updated_at         timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, supplier_id) REFERENCES suppliers (tenant_id, id),
    FOREIGN KEY (tenant_id, entity_id) REFERENCES legal_entities (tenant_id, id),
    FOREIGN KEY (tenant_id, duplicate_of) REFERENCES documents (tenant_id, id),
    CHECK (duplicate_of IS NULL OR duplicate_of::text <> id::text)
);

COMMENT ON TABLE documents IS
    'Understood documents (§7). Two live rows with the same supplier and invoice number are '
    'allowed on purpose: a duplicate with a different IBAN is a fraud signal, not an insert error (§26).';

-- Duplicate detection and matching lookups.
CREATE INDEX documents_supplier_invoice_idx ON documents (tenant_id, supplier_id, invoice_number)
    WHERE invoice_number IS NOT NULL;
CREATE INDEX documents_supplier_tax_invoice_idx ON documents (tenant_id, supplier_tax_id, invoice_number)
    WHERE invoice_number IS NOT NULL;
CREATE INDEX documents_issue_date_idx ON documents (tenant_id, issue_date);
CREATE INDEX documents_gross_idx ON documents (tenant_id, gross_amount) WHERE gross_amount IS NOT NULL;
CREATE INDEX documents_entity_idx ON documents (tenant_id, entity_id);

CREATE TRIGGER documents_touch BEFORE UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- models.Document.evidence_ids, in order (position 1 = the primary original).
CREATE TABLE document_evidence (
    tenant_id    tenant_key NOT NULL,
    document_id  bo_id      NOT NULL,
    evidence_id  bo_id      NOT NULL,
    position     smallint   NOT NULL CHECK (position >= 1),
    PRIMARY KEY (tenant_id, document_id, evidence_id),
    UNIQUE (tenant_id, document_id, position),
    FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, evidence_id) REFERENCES evidence (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX document_evidence_evidence_idx ON document_evidence (tenant_id, evidence_id);

-- --------------------------------------------------------------------- field-level verification

-- One observation of one field from one source (models.FieldObservation, §18):
-- value, source, method, confidence and location. Observations are facts and
-- are never edited; a correction is a new observation (method 'human').
-- Amounts are stored as JSON strings of the exact Decimal, never JSON numbers.
CREATE TABLE field_observations (
    tenant_id    tenant_key        NOT NULL,
    id           bigint            GENERATED ALWAYS AS IDENTITY,
    document_id  bo_id             NOT NULL,
    field_name   slug              NOT NULL,   -- models.CriticalField value or another field name
    value        jsonb             NOT NULL,
    source       nonblank_text     NOT NULL,   -- evidence id or engine ("ev_1@pp-ocrv6")
    evidence_id  bo_id,                        -- the original this was read from, if any
    method       extraction_method NOT NULL,
    confidence   numeric(5,4)      NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    location     jsonb             CHECK (location IS NULL OR jsonb_typeof(location) IN ('object', 'string')),
    observed_at  timestamptz       NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, document_id, field_name, id),
    FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id),
    FOREIGN KEY (tenant_id, evidence_id) REFERENCES evidence (tenant_id, id) ON DELETE CASCADE
);

COMMENT ON COLUMN field_observations.location IS
    'Bounding box {"page","x0","y0","x1","y1"} or a structured location string (§18).';

CREATE INDEX field_observations_field_idx ON field_observations (tenant_id, document_id, field_name);
CREATE INDEX field_observations_evidence_idx ON field_observations (tenant_id, evidence_id)
    WHERE evidence_id IS NOT NULL;

CREATE TRIGGER field_observations_append_only BEFORE UPDATE OR DELETE ON field_observations
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation_except_evidence_deletion();
CREATE TRIGGER field_observations_no_truncate BEFORE TRUNCATE ON field_observations
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- The current verdict for one field of one document (models.VerifiedField).
CREATE TABLE verified_fields (
    tenant_id    tenant_key    NOT NULL,
    document_id  bo_id         NOT NULL,
    name         slug          NOT NULL,
    value        jsonb         NOT NULL,
    quality      quality_level NOT NULL,
    reasons      text[]        NOT NULL DEFAULT '{}' CHECK (all_nonblank(reasons)),
    decided_at   timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, document_id, name),
    FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id) ON DELETE CASCADE
);

-- The observations a verdict was made from (VerifiedField.observations).
CREATE TABLE verified_field_observations (
    tenant_id       tenant_key NOT NULL,
    document_id     bo_id      NOT NULL,
    field_name      slug       NOT NULL,
    observation_id  bigint     NOT NULL,
    PRIMARY KEY (tenant_id, document_id, field_name, observation_id),
    FOREIGN KEY (tenant_id, document_id, field_name)
        REFERENCES verified_fields (tenant_id, document_id, name) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, document_id, field_name, observation_id)
        REFERENCES field_observations (tenant_id, document_id, field_name, id) ON DELETE CASCADE
);

CREATE INDEX verified_field_observations_obs_idx ON verified_field_observations (tenant_id, observation_id);

-- Floor for GREEN (§57): a verified field must rest on at least two distinct
-- observations (method + source). The verifier's own rule is stricter
-- (independent channels, one high-rank source); this only makes it impossible
-- to store a GREEN verdict with nothing behind it. Checked at commit, so a
-- verdict and its links can be written in any order. SECURITY DEFINER: a
-- deferred check runs as whoever commits (e.g. the evidence deletion role,
-- which may not read documents) and must not depend on that role's grants.
CREATE FUNCTION check_verified_field_support() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public
AS $$
DECLARE
    t text;
    d text;
    n text;
    sources integer;
BEGIN
    IF TG_TABLE_NAME = 'verified_fields' THEN
        t := NEW.tenant_id; d := NEW.document_id; n := NEW.name;
    ELSE
        t := OLD.tenant_id; d := OLD.document_id; n := OLD.field_name;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM verified_fields f
        WHERE f.tenant_id = t AND f.document_id = d AND f.name = n AND f.quality = 'verified'
    ) THEN
        RETURN NULL;
    END IF;
    SELECT count(DISTINCT (o.method, o.source)) INTO sources
    FROM verified_field_observations l
    JOIN field_observations o ON o.tenant_id = l.tenant_id AND o.id = l.observation_id
    WHERE l.tenant_id = t AND l.document_id = d AND l.field_name = n;
    IF sources < 2 THEN
        RAISE EXCEPTION 'field % of document % is verified with % supporting source(s); at least 2 are required',
            n, d, sources
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$$;

REVOKE EXECUTE ON FUNCTION check_verified_field_support() FROM PUBLIC;

CREATE CONSTRAINT TRIGGER verified_fields_need_support
    AFTER INSERT OR UPDATE ON verified_fields
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION check_verified_field_support();
CREATE CONSTRAINT TRIGGER verified_field_links_keep_support
    AFTER DELETE ON verified_field_observations
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION check_verified_field_support();

-- --------------------------------------------------------------------- transactions

-- Bank or card transaction (models.Transaction). Negative amount = money out.
CREATE TABLE transactions (
    tenant_id          tenant_key       NOT NULL REFERENCES tenants (id),
    id                 bo_id            NOT NULL,
    account_id         bo_id            NOT NULL,
    booked_on          date             NOT NULL,
    amount             numeric(18,2)    NOT NULL,
    currency           currency_code    NOT NULL DEFAULT 'EUR',
    counterparty       text             NOT NULL,
    description        text             NOT NULL DEFAULT '',
    kind               transaction_kind NOT NULL DEFAULT 'card',
    card_last4         char(4)          CHECK (card_last4 ~ '^[0-9]{4}$'),
    counterparty_iban  iban_code,
    reference          text,
    entity_id          bo_id,
    evidence_id        bo_id,           -- the raw bank/card record, when stored as evidence
    external_id        text,            -- the bank's own id, for idempotent imports
    created_at         timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, account_id) REFERENCES accounts (tenant_id, id),
    FOREIGN KEY (tenant_id, entity_id) REFERENCES legal_entities (tenant_id, id),
    FOREIGN KEY (tenant_id, evidence_id) REFERENCES evidence (tenant_id, id) ON DELETE SET NULL (evidence_id)
);

CREATE UNIQUE INDEX transactions_external_id_key ON transactions (tenant_id, account_id, external_id)
    WHERE external_id IS NOT NULL;
CREATE INDEX transactions_booked_on_idx ON transactions (tenant_id, booked_on);
CREATE INDEX transactions_account_booked_idx ON transactions (tenant_id, account_id, booked_on);
CREATE INDEX transactions_amount_idx ON transactions (tenant_id, amount, booked_on);
CREATE INDEX transactions_counterparty_iban_idx ON transactions (tenant_id, counterparty_iban)
    WHERE counterparty_iban IS NOT NULL;

-- --------------------------------------------------------------------- matches

-- A reconciliation result (backoffice.reconciliation.engine.Match). A match is
-- never rewritten: a better answer supersedes it with a new match.
CREATE TABLE matches (
    tenant_id        tenant_key    NOT NULL REFERENCES tenants (id),
    id               bo_id         NOT NULL,
    kind             match_kind    NOT NULL,
    quality          quality_level NOT NULL,
    points           integer       NOT NULL,
    headline         nonblank_text NOT NULL,
    currency         currency_code NOT NULL DEFAULT 'EUR',  -- documents' currency
    bank_currency    currency_code NOT NULL DEFAULT 'EUR',
    fee              numeric(18,2) NOT NULL DEFAULT 0,       -- in bank_currency
    conversion_cost  numeric(18,2),                          -- in bank_currency
    fx_rate          numeric(20,10) CHECK (fx_rate > 0),
    difference       numeric(18,2) NOT NULL DEFAULT 0,
    tags             slug[]        NOT NULL DEFAULT '{}',    -- MatchTag values
    status           text          NOT NULL DEFAULT 'proposed'
                     CHECK (status IN ('proposed', 'confirmed', 'rejected', 'superseded')),
    superseded_by    bo_id,
    decided_by       nonblank_text,
    decided_at       timestamptz,
    created_at       timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, superseded_by) REFERENCES matches (tenant_id, id),
    CHECK ((status = 'superseded') = (superseded_by IS NOT NULL)),
    CHECK (status = 'proposed' OR decided_at IS NOT NULL)
);

CREATE INDEX matches_status_idx ON matches (tenant_id, status);

CREATE TABLE match_transactions (
    tenant_id       tenant_key NOT NULL,
    match_id        bo_id      NOT NULL,
    transaction_id  bo_id      NOT NULL,
    settled         boolean    NOT NULL DEFAULT false,  -- Match.settled_transaction_ids
    PRIMARY KEY (tenant_id, match_id, transaction_id),
    FOREIGN KEY (tenant_id, match_id) REFERENCES matches (tenant_id, id),
    FOREIGN KEY (tenant_id, transaction_id) REFERENCES transactions (tenant_id, id)
);

CREATE INDEX match_transactions_tx_idx ON match_transactions (tenant_id, transaction_id);

CREATE TABLE match_documents (
    tenant_id    tenant_key NOT NULL,
    match_id     bo_id      NOT NULL,
    document_id  bo_id      NOT NULL,
    PRIMARY KEY (tenant_id, match_id, document_id),
    FOREIGN KEY (tenant_id, match_id) REFERENCES matches (tenant_id, id),
    FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id)
);

CREATE INDEX match_documents_doc_idx ON match_documents (tenant_id, document_id);

-- How much of each payment settles each document (Match.allocations).
CREATE TABLE match_allocations (
    tenant_id       tenant_key    NOT NULL,
    match_id        bo_id         NOT NULL,
    transaction_id  bo_id         NOT NULL,
    document_id     bo_id         NOT NULL,
    amount          numeric(18,2) NOT NULL,
    currency        currency_code NOT NULL,
    PRIMARY KEY (tenant_id, match_id, transaction_id, document_id),
    FOREIGN KEY (tenant_id, match_id, transaction_id)
        REFERENCES match_transactions (tenant_id, match_id, transaction_id),
    FOREIGN KEY (tenant_id, match_id, document_id)
        REFERENCES match_documents (tenant_id, match_id, document_id)
);

-- The "Why?" lines of a match (§54): "Invoice total €83.21, bank charge
-- €83.21", "Dates 1 day apart", "Supplier VAT matches". Written once.
CREATE TABLE match_factors (
    tenant_id     tenant_key    NOT NULL,
    match_id      bo_id         NOT NULL,
    position      smallint      NOT NULL CHECK (position >= 1),
    line          nonblank_text NOT NULL,   -- plain words shown to people
    factor        slug,                      -- optional machine code: amount, date, supplier_tax_id, card...
    points        integer,
    evidence_ids  text[]        NOT NULL DEFAULT '{}' CHECK (all_nonblank(evidence_ids)),
    PRIMARY KEY (tenant_id, match_id, position),
    FOREIGN KEY (tenant_id, match_id) REFERENCES matches (tenant_id, id)
);

CREATE TRIGGER match_factors_append_only BEFORE UPDATE OR DELETE ON match_factors
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER match_factors_no_truncate BEFORE TRUNCATE ON match_factors
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- --------------------------------------------------------------------- evidence deletion executor

-- Executes an approved deletion request for the tenant in app.tenant_id and
-- returns the object key the workflow must purge from the object store
-- (governance-mode bypass, §52). Everything derived only from that original
-- goes with it (sightings, observations, links, embeddings). What it supported
-- can only lose confidence, never gain it (§57): verified fields and
-- documents it backed drop from GREEN to AMBER for re-verification.
CREATE FUNCTION execute_evidence_deletion(p_request_id text, p_executed_by text) RETURNS text
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public
AS $$
DECLARE
    tenant text := nullif(current_setting('app.tenant_id', true), '');
    req    evidence_deletion_requests%ROWTYPE;
BEGIN
    IF tenant IS NULL THEN
        RAISE EXCEPTION 'app.tenant_id is not set' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_executed_by IS NULL OR p_executed_by !~ '\S' THEN
        RAISE EXCEPTION 'executed_by is required' USING ERRCODE = 'not_null_violation';
    END IF;

    SELECT * INTO req FROM evidence_deletion_requests
    WHERE tenant_id = tenant AND id = p_request_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no deletion request %', p_request_id USING ERRCODE = 'no_data_found';
    END IF;
    IF req.status <> 'approved' THEN
        RAISE EXCEPTION 'deletion request % is %, not approved', req.id, req.status
            USING ERRCODE = 'restrict_violation';
    END IF;
    PERFORM 1 FROM evidence
    WHERE tenant_id = tenant AND id = req.evidence_id AND sha256 = req.evidence_sha256
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'evidence % no longer matches the request', req.evidence_id
            USING ERRCODE = 'no_data_found';
    END IF;

    UPDATE verified_fields f
    SET quality = 'likely',
        reasons = f.reasons || 'The original document was deleted.'::text,
        decided_at = now()
    WHERE f.tenant_id = tenant
      AND f.quality = 'verified'
      AND EXISTS (
          SELECT 1
          FROM verified_field_observations l
          JOIN field_observations o ON o.tenant_id = l.tenant_id AND o.id = l.observation_id
          WHERE l.tenant_id = f.tenant_id AND l.document_id = f.document_id
            AND l.field_name = f.name AND o.evidence_id = req.evidence_id
      );
    UPDATE documents d
    SET quality = 'likely'
    WHERE d.tenant_id = tenant
      AND d.quality = 'verified'
      AND EXISTS (
          SELECT 1 FROM document_evidence de
          WHERE de.tenant_id = d.tenant_id AND de.document_id = d.id
            AND de.evidence_id = req.evidence_id
      );

    PERFORM set_config('backoffice.evidence_deletion_request', req.id, true);
    DELETE FROM evidence WHERE tenant_id = tenant AND id = req.evidence_id;
    PERFORM set_config('backoffice.evidence_deletion_request', '', true);

    UPDATE evidence_deletion_requests
    SET status = 'executed', executed_by = p_executed_by, executed_at = now()
    WHERE tenant_id = tenant AND id = req.id;
    RETURN req.storage_key;
END
$$;

REVOKE EXECUTE ON FUNCTION execute_evidence_deletion(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION execute_evidence_deletion(text, text) TO backoffice_evidence_admin;

-- --------------------------------------------------------------------- isolation and grants

CALL enable_tenant_isolation('documents');
CALL enable_tenant_isolation('document_evidence');
CALL enable_tenant_isolation('field_observations');
CALL enable_tenant_isolation('verified_fields');
CALL enable_tenant_isolation('verified_field_observations');
CALL enable_tenant_isolation('transactions');
CALL enable_tenant_isolation('matches');
CALL enable_tenant_isolation('match_transactions');
CALL enable_tenant_isolation('match_documents');
CALL enable_tenant_isolation('match_allocations');
CALL enable_tenant_isolation('match_factors');

GRANT SELECT, INSERT, UPDATE ON documents, matches, transactions TO backoffice_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON document_evidence, verified_fields,
    verified_field_observations TO backoffice_app;
GRANT SELECT, INSERT ON field_observations, match_transactions, match_documents,
    match_allocations, match_factors TO backoffice_app;

GRANT SELECT ON documents, document_evidence, field_observations, verified_fields,
    verified_field_observations, transactions, matches, match_transactions,
    match_documents, match_allocations, match_factors TO backoffice_readonly;
