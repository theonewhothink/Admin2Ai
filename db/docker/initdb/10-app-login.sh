#!/bin/sh
# Local development only (docker-entrypoint-initdb.d of the postgres service).
#
# Creates the login user the api and worker connect with: a member of
# backoffice_app, never a superuser, never the table owner, never BYPASSRLS,
# so row-level security applies to every query it makes (§52). The group roles
# are created here too, idempotently, so the grant works before the first
# migration; migration 0001 accepts roles that already exist.
set -eu
: "${BACKOFFICE_DB_APP_PASSWORD:?set BACKOFFICE_DB_APP_PASSWORD}"

psql -v ON_ERROR_STOP=1 --no-psqlrc \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --set=app_password="$BACKOFFICE_DB_APP_PASSWORD" <<'SQL'
DO $$
DECLARE
    r text;
BEGIN
    FOREACH r IN ARRAY ARRAY[
        'backoffice_app', 'backoffice_readonly',
        'backoffice_evidence_admin', 'backoffice_scheduler'
    ] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN NOBYPASSRLS', r);
        END IF;
    END LOOP;
END
$$;

SELECT format(
    'CREATE ROLE backoffice_api LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD %L IN ROLE backoffice_app',
    :'app_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'backoffice_api')
\gexec
SQL
