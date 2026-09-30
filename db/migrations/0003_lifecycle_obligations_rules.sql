-- 0003_lifecycle_obligations_rules: the golden-rule state machine (§3,
-- domain/lifecycle.py), obligations (§24), learned rules (§28, §38) and the
-- owner's "Needs You" questions and answers (§5, §37).

-- --------------------------------------------------------------------- enumerations

CREATE DOMAIN item_stage AS text CHECK (VALUE IN (
    'discovered', 'acquired', 'understood', 'verified', 'matched', 'acted',
    'confirmed', 'closed', 'needs_owner', 'conflict', 'not_required'
));

CREATE DOMAIN obligation_kind AS text CHECK (VALUE IN (
    'tax_deadline', 'government_request', 'kyc_request', 'license_renewal',
    'insurance_renewal', 'contract_renewal', 'filing', 'rent', 'debt_collection',
    'bank_request', 'payment_deadline'
));

CREATE DOMAIN subject_type AS text CHECK (VALUE IN ('transaction', 'document', 'obligation'));

-- --------------------------------------------------------------------- obligations

CREATE TABLE obligations (
    tenant_id               tenant_key      NOT NULL REFERENCES tenants (id),
    id                      bo_id           NOT NULL,
    entity_id               bo_id           NOT NULL,
    kind                    obligation_kind NOT NULL,
    title                   nonblank_text   NOT NULL,
    due_on                  date            NOT NULL,
    amount                  numeric(18,2),
    currency                currency_code,
    responsible             nonblank_text   NOT NULL DEFAULT 'owner',
    consequence             text            NOT NULL DEFAULT '',
    required_evidence       text            NOT NULL DEFAULT '',
    verification_condition  text            NOT NULL DEFAULT '',
    created_at              timestamptz     NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, entity_id) REFERENCES legal_entities (tenant_id, id),
    CHECK (amount IS NULL OR currency IS NOT NULL)
);

CREATE INDEX obligations_due_idx ON obligations (tenant_id, due_on);
CREATE INDEX obligations_entity_idx ON obligations (tenant_id, entity_id, due_on);

-- models.Obligation.satisfied_by_evidence_ids
CREATE TABLE obligation_evidence (
    tenant_id      tenant_key  NOT NULL,
    obligation_id  bo_id       NOT NULL,
    evidence_id    bo_id       NOT NULL,
    added_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, obligation_id, evidence_id),
    FOREIGN KEY (tenant_id, obligation_id) REFERENCES obligations (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, evidence_id) REFERENCES evidence (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX obligation_evidence_evidence_idx ON obligation_evidence (tenant_id, evidence_id);

-- --------------------------------------------------------------------- tracked items (§3)

-- Any administrative event moving toward closure (lifecycle.TrackedItem).
-- The database holds the same invariants as the Python model:
--   * closed => GREEN, conflict => RED, no RED on the golden path;
--   * every change of stage or quality is a transition row carrying an actor
--     and at least one evidence id, and the item always equals its latest
--     transition (checked at commit, so the item and its transition can be
--     written in either order inside one transaction).
-- The full move rules (no skipping, resume after a side state...) are
-- enforced by TrackedItem.advance before anything is written.
CREATE TABLE tracked_items (
    tenant_id     tenant_key    NOT NULL REFERENCES tenants (id),
    id            bo_id         NOT NULL,
    subject_type  subject_type  NOT NULL,
    subject_id    bo_id         NOT NULL,
    stage         item_stage    NOT NULL DEFAULT 'discovered',
    quality       quality_level NOT NULL DEFAULT 'likely',
    created_at    timestamptz   NOT NULL DEFAULT now(),
    updated_at    timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, subject_type, subject_id),
    CONSTRAINT closed_is_verified CHECK (stage <> 'closed' OR quality = 'verified'),
    CONSTRAINT conflict_is_red CHECK (stage <> 'conflict' OR quality = 'conflict'),
    CONSTRAINT golden_path_not_red CHECK (
        stage NOT IN ('discovered', 'acquired', 'understood', 'verified', 'matched',
                      'acted', 'confirmed', 'closed')
        OR quality <> 'conflict'
    )
);

CREATE INDEX tracked_items_stage_idx ON tracked_items (tenant_id, stage);

CREATE TRIGGER tracked_items_touch BEFORE UPDATE ON tracked_items
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE tracked_item_transitions (
    tenant_id     tenant_key    NOT NULL,
    item_id       bo_id         NOT NULL,
    seq           integer       NOT NULL CHECK (seq >= 1),
    at            timestamptz   NOT NULL DEFAULT now(),
    from_stage    item_stage,
    to_stage      item_stage    NOT NULL,
    actor         nonblank_text NOT NULL,
    evidence_ids  text[]        NOT NULL CHECK (cardinality(evidence_ids) >= 1 AND all_nonblank(evidence_ids)),
    note          text          NOT NULL DEFAULT '',
    quality       quality_level,           -- NULL only for legacy records
    PRIMARY KEY (tenant_id, item_id, seq),
    FOREIGN KEY (tenant_id, item_id) REFERENCES tracked_items (tenant_id, id),
    -- IS NOT DISTINCT FROM: a legacy NULL quality must not satisfy these checks.
    CONSTRAINT transition_closed_is_verified CHECK (to_stage <> 'closed' OR quality IS NOT DISTINCT FROM 'verified'),
    CONSTRAINT transition_conflict_is_red CHECK (to_stage <> 'conflict' OR quality IS NOT DISTINCT FROM 'conflict')
);

COMMENT ON TABLE tracked_item_transitions IS
    'Append-only history of lifecycle.Transition. Nothing closes without evidence (§3).';

CREATE TRIGGER tracked_item_transitions_append_only BEFORE UPDATE OR DELETE ON tracked_item_transitions
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER tracked_item_transitions_no_truncate BEFORE TRUNCATE ON tracked_item_transitions
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- A transition continues the history: next seq, from the stage the previous
-- transition reached. Order is seq, not the clock (workers' clocks may drift).
CREATE FUNCTION check_transition_sequence() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public
AS $$
DECLARE
    prev tracked_item_transitions%ROWTYPE;
BEGIN
    SELECT * INTO prev FROM tracked_item_transitions t
    WHERE t.tenant_id = NEW.tenant_id AND t.item_id = NEW.item_id
    ORDER BY t.seq DESC
    LIMIT 1;
    IF NOT FOUND THEN
        IF NEW.seq <> 1 THEN
            RAISE EXCEPTION 'the first transition of item % must have seq 1', NEW.item_id
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.seq <> prev.seq + 1 THEN
        RAISE EXCEPTION 'transition % of item % does not follow %', NEW.seq, NEW.item_id, prev.seq
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.from_stage IS DISTINCT FROM prev.to_stage THEN
        RAISE EXCEPTION 'item % was %, not %', NEW.item_id, prev.to_stage, NEW.from_stage
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER tracked_item_transitions_sequence BEFORE INSERT ON tracked_item_transitions
    FOR EACH ROW EXECUTE FUNCTION check_transition_sequence();

-- At commit: the item equals its latest transition (or, with no history, is
-- still at the start). This is what makes "closed without evidence"
-- impossible to store.
CREATE FUNCTION check_item_matches_history() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public
AS $$
DECLARE
    t      text;
    i      text;
    item   tracked_items%ROWTYPE;
    latest tracked_item_transitions%ROWTYPE;
BEGIN
    IF TG_TABLE_NAME = 'tracked_items' THEN
        t := NEW.tenant_id; i := NEW.id;
    ELSE
        t := NEW.tenant_id; i := NEW.item_id;
    END IF;
    SELECT * INTO item FROM tracked_items WHERE tenant_id = t AND id = i;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    SELECT * INTO latest FROM tracked_item_transitions
    WHERE tenant_id = t AND item_id = i
    ORDER BY seq DESC
    LIMIT 1;
    IF NOT FOUND THEN
        IF item.stage <> 'discovered' THEN
            RAISE EXCEPTION 'item % is % without any recorded transition', i, item.stage
                USING ERRCODE = 'check_violation',
                      HINT = 'Every step needs a transition with evidence (§3).';
        END IF;
        RETURN NULL;
    END IF;
    IF latest.to_stage <> item.stage
       OR (latest.quality IS NOT NULL AND latest.quality <> item.quality) THEN
        RAISE EXCEPTION 'item % is %/% but its latest transition says %/%',
            i, item.stage, item.quality, latest.to_stage, coalesce(latest.quality, '?')
            USING ERRCODE = 'check_violation',
                  HINT = 'Change stage or quality only together with a new transition.';
    END IF;
    RETURN NULL;
END
$$;

CREATE CONSTRAINT TRIGGER tracked_items_match_history
    AFTER INSERT OR UPDATE ON tracked_items
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION check_item_matches_history();
CREATE CONSTRAINT TRIGGER tracked_item_transitions_match_item
    AFTER INSERT ON tracked_item_transitions
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION check_item_matches_history();

-- --------------------------------------------------------------------- rules

-- Learned rules (backoffice.learning.rules.Rule). Owner rules belong to one
-- tenant; an accountant rule is for one client or for all their clients, in
-- which case tenant_id is NULL and the rule is visible in every tenant where
-- that accountant is an active member (§28 "scoped to one client or all
-- authorized clients").
CREATE TABLE rules (
    id              bo_id         PRIMARY KEY,
    tenant_id       tenant_key    REFERENCES tenants (id),
    author          text          NOT NULL CHECK (author IN ('owner', 'accountant')),
    author_id       bo_id         NOT NULL,
    scope           text          NOT NULL CHECK (scope IN ('tenant', 'client', 'all_clients_of_accountant')),
    match           jsonb         NOT NULL CHECK (jsonb_typeof(match) = 'object'),
    outcome         jsonb         NOT NULL CHECK (jsonb_typeof(outcome) = 'object'),
    label           text          NOT NULL DEFAULT '',
    active          boolean       NOT NULL DEFAULT true,
    replaced_by     text[]        NOT NULL DEFAULT '{}' CHECK (all_nonblank(replaced_by)),
    created_at      timestamptz   NOT NULL DEFAULT now(),
    deactivated_at  timestamptz,
    CONSTRAINT owner_rules_are_tenant_scoped CHECK (author <> 'owner' OR scope = 'tenant'),
    CONSTRAINT accountant_rules_are_client_scoped CHECK (author <> 'accountant' OR scope <> 'tenant'),
    CONSTRAINT scope_matches_tenant CHECK ((scope = 'all_clients_of_accountant') = (tenant_id IS NULL)),
    CHECK (active OR deactivated_at IS NOT NULL)
);

CREATE INDEX rules_tenant_idx ON rules (tenant_id) WHERE active;
CREATE INDEX rules_accountant_idx ON rules (author_id) WHERE active AND tenant_id IS NULL;

ALTER TABLE rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE rules FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON rules
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY accountant_rules_visible ON rules FOR SELECT
    USING (
        tenant_id IS NULL
        AND EXISTS (
            SELECT 1 FROM memberships m
            WHERE m.tenant_id = current_setting('app.tenant_id', true)
              AND m.user_id = rules.author_id
              AND m.role = 'accountant'
              AND m.revoked_at IS NULL
        )
    );
CREATE POLICY accountant_rules_own ON rules
    USING (tenant_id IS NULL AND author_id = current_setting('app.user_id', true))
    WITH CHECK (tenant_id IS NULL AND author = 'accountant'
                AND author_id = current_setting('app.user_id', true));

-- --------------------------------------------------------------------- needs you (§37)

CREATE TABLE needs_you_questions (
    tenant_id     tenant_key    NOT NULL REFERENCES tenants (id),
    id            bo_id         NOT NULL,
    kind          text          NOT NULL
                  CHECK (kind IN ('which_company', 'what_is_this', 'missing_invoice', 'confirm_match')),
    prompt        nonblank_text NOT NULL,   -- plain words, no jargon (§36)
    detail        text          NOT NULL DEFAULT '',
    options       jsonb         NOT NULL CHECK (jsonb_typeof(options) = 'array' AND jsonb_array_length(options) >= 1),
    why           text[]        NOT NULL DEFAULT '{}' CHECK (all_nonblank(why)),
    facts         jsonb         NOT NULL CHECK (jsonb_typeof(facts) = 'object'),
    subject_type  text          NOT NULL CHECK (subject_type IN ('transaction', 'document', 'series')),
    subject_id    bo_id         NOT NULL,
    series_key    text,
    item_id       bo_id,        -- the tracked item waiting on this answer, if any
    status        text          NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'answered', 'withdrawn')),
    created_at    timestamptz   NOT NULL DEFAULT now(),
    updated_at    timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, item_id) REFERENCES tracked_items (tenant_id, id),
    CONSTRAINT options_have_ids CHECK (NOT jsonb_path_exists(options, '$[*] ? (!(exists(@.id)))'))
);

CREATE INDEX needs_you_open_idx ON needs_you_questions (tenant_id, created_at) WHERE status = 'open';
CREATE INDEX needs_you_subject_idx ON needs_you_questions (tenant_id, subject_type, subject_id);

CREATE TRIGGER needs_you_questions_touch BEFORE UPDATE ON needs_you_questions
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Answers are facts: a changed mind is a new answer, the old one stays.
CREATE TABLE needs_you_answers (
    tenant_id    tenant_key    NOT NULL,
    id           bigint        GENERATED ALWAYS AS IDENTITY,
    question_id  bo_id         NOT NULL,
    option_id    nonblank_text NOT NULL,
    answered_by  bo_id         NOT NULL REFERENCES users (id),
    answered_at  timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, question_id) REFERENCES needs_you_questions (tenant_id, id)
);

CREATE INDEX needs_you_answers_question_idx ON needs_you_answers (tenant_id, question_id, answered_at);

CREATE FUNCTION check_answer_option() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM needs_you_questions q, jsonb_array_elements(q.options) AS o
        WHERE q.tenant_id = NEW.tenant_id AND q.id = NEW.question_id AND o ->> 'id' = NEW.option_id
    ) THEN
        RAISE EXCEPTION 'option % is not one of the choices of question %', NEW.option_id, NEW.question_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER needs_you_answers_option BEFORE INSERT ON needs_you_answers
    FOR EACH ROW EXECUTE FUNCTION check_answer_option();
CREATE TRIGGER needs_you_answers_append_only BEFORE UPDATE OR DELETE ON needs_you_answers
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER needs_you_answers_no_truncate BEFORE TRUNCATE ON needs_you_answers
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- --------------------------------------------------------------------- isolation and grants

CALL enable_tenant_isolation('obligations');
CALL enable_tenant_isolation('obligation_evidence');
CALL enable_tenant_isolation('tracked_items');
CALL enable_tenant_isolation('tracked_item_transitions');
CALL enable_tenant_isolation('needs_you_questions');
CALL enable_tenant_isolation('needs_you_answers');

GRANT SELECT, INSERT, UPDATE ON obligations, tracked_items, needs_you_questions TO backoffice_app;
GRANT SELECT, INSERT, DELETE ON obligation_evidence TO backoffice_app;
GRANT SELECT, INSERT ON tracked_item_transitions, needs_you_answers TO backoffice_app;
GRANT SELECT, INSERT ON rules TO backoffice_app;
GRANT UPDATE (active, deactivated_at, replaced_by, label) ON rules TO backoffice_app;

GRANT SELECT ON obligations, obligation_evidence, tracked_items, tracked_item_transitions,
    rules, needs_you_questions, needs_you_answers TO backoffice_readonly;
