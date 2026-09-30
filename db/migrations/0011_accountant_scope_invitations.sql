-- 0011_accountant_scope_invitations: an accountant per company, and clients
-- invited by their accountant (§28, §29, §51).
--
-- A business may have several companies, each with its own accountant. An
-- accountant membership can therefore be limited to some companies of the
-- business (memberships.company_ids; empty = every company). The API filters
-- every accountant read to those companies.
--
-- An accountant invites a client business by email ("Your accountant has
-- enabled Back Office for you."). The email carries a single-use token; only
-- its SHA-256 is stored here. The invitation expires, and it can be accepted
-- once, by the owner whose email it was sent to: that grants the inviter an
-- accountant membership in the accepting business (limited to the companies
-- whose NIFs the invitation names, when it names any).
--
-- Row-level security: the inviter reads and records their own invitations
-- from their business (app.tenant_id and app.user_id); whoever presents a
-- token reads and accepts that one invitation (app.invite_hash, the SHA-256
-- of the token presented). With neither set, nothing is visible.

-- --------------------------------------------------------------------- company-limited memberships

ALTER TABLE memberships ADD COLUMN company_ids text[] NOT NULL DEFAULT '{}';
ALTER TABLE memberships ADD CONSTRAINT memberships_company_ids_check
    CHECK (cardinality(company_ids) = 0 OR (role = 'accountant' AND array_position(company_ids, NULL) IS NULL));

COMMENT ON COLUMN memberships.company_ids IS
    'Accountant memberships only: the companies of the business this accountant may see. Empty = every company.';

GRANT UPDATE (company_ids) ON memberships TO backoffice_app;

-- --------------------------------------------------------------------- client invitations

CREATE TABLE accountant_invitations (
    id                  bo_id         PRIMARY KEY,
    token_hash          sha256_hex    NOT NULL UNIQUE,   -- SHA-256 of the token; the token is only in the email
    inviter_tenant_id   tenant_key    NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    inviter_user_id     bo_id         NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    inviter_email       nonblank_text NOT NULL CHECK (inviter_email = lower(inviter_email)),
    inviter_name        text          NOT NULL DEFAULT '',
    firm                text          NOT NULL DEFAULT '',
    email               nonblank_text NOT NULL CHECK (email = lower(email)),
    client_name         text          NOT NULL DEFAULT '',
    tax_ids             text[]        NOT NULL DEFAULT '{}' CHECK (array_position(tax_ids, NULL) IS NULL),
    created_at          timestamptz   NOT NULL,
    expires_at          timestamptz   NOT NULL,
    sent_at             timestamptz,
    accepted_at         timestamptz,
    accepted_tenant_id  tenant_key    REFERENCES tenants (id) ON DELETE SET NULL,
    accepted_by         bo_id         REFERENCES users (id) ON DELETE SET NULL,
    CHECK (expires_at > created_at),
    CHECK (sent_at IS NULL OR sent_at >= created_at),
    CHECK (accepted_at IS NULL OR accepted_at >= created_at)
);

COMMENT ON TABLE accountant_invitations IS
    'Client businesses invited by an accountant (§29). Single use, expiring; found by the hash of the token presented.';

CREATE INDEX accountant_invitations_inviter_idx ON accountant_invitations (inviter_tenant_id, inviter_user_id);

ALTER TABLE accountant_invitations ENABLE ROW LEVEL SECURITY;
ALTER TABLE accountant_invitations FORCE ROW LEVEL SECURITY;
CREATE POLICY inviter ON accountant_invitations
    USING (inviter_tenant_id = current_setting('app.tenant_id', true)
           AND inviter_user_id = current_setting('app.user_id', true))
    WITH CHECK (inviter_tenant_id = current_setting('app.tenant_id', true)
                AND inviter_user_id = current_setting('app.user_id', true));
CREATE POLICY presented_invitation ON accountant_invitations
    USING (token_hash = current_setting('app.invite_hash', true))
    WITH CHECK (token_hash = current_setting('app.invite_hash', true));

GRANT SELECT, INSERT ON accountant_invitations TO backoffice_app;
GRANT UPDATE (sent_at, accepted_at, accepted_tenant_id, accepted_by) ON accountant_invitations TO backoffice_app;
GRANT SELECT ON accountant_invitations TO backoffice_readonly;
