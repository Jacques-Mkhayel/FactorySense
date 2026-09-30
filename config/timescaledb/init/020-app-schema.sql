-- Runs once, after 010-telemetry.sh, on the first start with an empty timescaledb-data volume.
-- TODO(phase 2): tables for assets, users, alerts (with source = 'equipment' | 'ids') and work orders,
-- plus least-privilege roles for the ingestor, rules-engine and api instead of the superuser.
-- FactorySense application schema: relational tables for Phase 2.
-- Alerts table stores equipment anomaly alerts and Suricata IDS alerts alike.

CREATE TABLE IF NOT EXISTS alerts (
    id          BIGSERIAL PRIMARY KEY,
    time        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source      TEXT NOT NULL CHECK (source IN ('equipment', 'ids')),
    site_id     TEXT,
    machine_id  TEXT,
    rule_name   TEXT NOT NULL,
    severity    TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'acknowledged', 'resolved')),
    details     JSONB NOT NULL DEFAULT '{}'::jsonb,
    dedupe_key  TEXT,
    count       INTEGER NOT NULL DEFAULT 1,
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_alerts_source ON alerts (source);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts (status);
CREATE INDEX IF NOT EXISTS idx_alerts_time ON alerts (time DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_dedupe ON alerts (dedupe_key, status) WHERE status = 'active';