-- 0006: sealed sign-in secrets for connections (§47 stay connected, §52 KMS / secrets).
--
-- Holds only envelope-encrypted material produced by
-- backend/src/backoffice/connectors/vault.py: the plaintext refresh token,
-- app password or consent id is never stored. wrapped_key is the per-record
-- data key wrapped by KMS with encryption context {tenant, connection,
-- provider}; ciphertext is AES-256-GCM with the same context as associated
-- data, so a row copied to another tenant or connection cannot be opened.

CREATE TABLE connection_credentials (
    tenant_id      text        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    connection_id  text        NOT NULL,
    provider       text        NOT NULL CHECK (provider IN ('google', 'microsoft', 'imap', 'open_banking', 'portal')),
    key_id         text        NOT NULL,
    wrapped_key    bytea       NOT NULL,
    nonce          bytea       NOT NULL CHECK (octet_length(nonce) = 12),
    ciphertext     bytea       NOT NULL,
    version        integer     NOT NULL DEFAULT 1 CHECK (version >= 1),
    expires_at     timestamptz,          -- when the grant itself ends (e.g. PSD2 consent), if known
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, connection_id)
);

CREATE INDEX connection_credentials_expiring ON connection_credentials (expires_at) WHERE expires_at IS NOT NULL;

CALL enable_tenant_isolation('connection_credentials');

COMMENT ON TABLE connection_credentials IS
    'Envelope-encrypted connection secrets. Deleting a connection deletes its row (sign-in destroyed).';
