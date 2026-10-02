#!/bin/sh
# Runs once, after 020-app-schema.sql, on the first start with an empty timescaledb-data volume.
# Dashboard users and their sessions, plus a least-privilege `api` role so the query layer stops
# using the superuser. A shell script (not .sql) because the role password comes from the environment.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v api_password="$API_DB_PASSWORD" -v db="$POSTGRES_DB" <<'SQL'
-- No public sign-up: an admin creates every account (user registration flows are out of scope).
CREATE TABLE users (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE CHECK (username ~ '^[a-z0-9._-]{3,32}$'),
    password_hash TEXT NOT NULL,                 -- scrypt, see services/api/src/auth.py
    role          TEXT NOT NULL CHECK (role IN ('admin', 'analyst')),
    active        BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login    TIMESTAMPTZ
);

-- Server-side sessions: logout and deactivation take effect at once, on every api replica.
-- Only a SHA-256 of the cookie is stored, so a database leak does not hand out live sessions.
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX ON sessions (expires_at);

-- The api reads telemetry and alerts, may only change an alert's workflow columns, and owns users.
CREATE ROLE api LOGIN PASSWORD :'api_password';
GRANT CONNECT ON DATABASE :"db" TO api;
GRANT USAGE ON SCHEMA public TO api;
GRANT SELECT ON telemetry, telemetry_1h, alerts TO api;
GRANT UPDATE (status, resolved_at, acknowledged_at, acknowledged_by) ON alerts TO api;
GRANT SELECT, INSERT, UPDATE ON users TO api;
GRANT SELECT, INSERT, DELETE ON sessions TO api;
SQL
