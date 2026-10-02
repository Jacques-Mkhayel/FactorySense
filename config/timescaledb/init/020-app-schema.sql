-- FactorySense application schema. Runs once, after 010-telemetry.sh, on the first start with an
-- empty timescaledb-data volume. This file is the single source of truth: services never run DDL.
-- TODO: assets, users and work orders tables, plus least-privilege roles for the ingestor,
-- rules-engine and api instead of the superuser.

-- Equipment anomalies and Suricata detections share one table, told apart by `source`, so the
-- dashboard lists them side by side and plain SQL can join them with telemetry.
CREATE TABLE IF NOT EXISTS alerts (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    time        TIMESTAMPTZ NOT NULL DEFAULT now(),  -- first occurrence
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),  -- latest occurrence
    resolved_at TIMESTAMPTZ,
    source      TEXT NOT NULL CHECK (source IN ('equipment', 'ids')),
    site_id     TEXT,
    machine_id  TEXT,                                -- NULL for IDS alerts
    rule_name   TEXT NOT NULL,
    severity    TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'acknowledged', 'resolved')),
    details     JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- What makes two events "the same alert": equipment:<site>:<machine>:<metric> or ids:<sid>:<src>-><dst>.
    dedupe_key  TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1 CHECK (count >= 1),
    CHECK (last_seen >= time),
    CHECK ((status = 'resolved') = (resolved_at IS NOT NULL))
);

-- At most one active alert per key. Enforced by the database rather than the code because the
-- rules-engine writes from several threads; it is also the arbiter of its INSERT ... ON CONFLICT.
CREATE UNIQUE INDEX IF NOT EXISTS alerts_active_dedupe_key ON alerts (dedupe_key) WHERE status = 'active';

-- Dashboard queries: newest first, filtered by status or by source.
CREATE INDEX IF NOT EXISTS alerts_status_time ON alerts (status, time DESC);
CREATE INDEX IF NOT EXISTS alerts_source_time ON alerts (source, time DESC);
