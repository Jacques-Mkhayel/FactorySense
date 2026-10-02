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
    acknowledged_at TIMESTAMPTZ,
    acknowledged_by TEXT,                            -- username of the analyst, for the audit trail
    source     TEXT NOT NULL CHECK (source IN ('equipment', 'ids')),
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

-- At most one open (active or acknowledged) alert per key. Enforced by the database rather than the
-- code because the rules-engine writes from several threads; it is also the arbiter of its
-- INSERT ... ON CONFLICT. Acknowledging keeps the alert open: repeats still bump its count and it
-- still auto-resolves, instead of a fresh active alert popping up a second after the analyst acks.
CREATE UNIQUE INDEX IF NOT EXISTS alerts_open_dedupe_key ON alerts (dedupe_key) WHERE status <> 'resolved';

-- Dashboard queries: newest first, filtered by status or by source.
CREATE INDEX IF NOT EXISTS alerts_status_time ON alerts (status, time DESC);
CREATE INDEX IF NOT EXISTS alerts_source_time ON alerts (source, time DESC);

-- Push every alert change to whoever LISTENs on 'alerts': each api replica holds one listener and
-- fans the row out to its WebSocket clients, so the dashboard is live without polling the table.
CREATE OR REPLACE FUNCTION notify_alert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('alerts', row_to_json(NEW)::text);
    RETURN NEW;
END $$;

CREATE OR REPLACE TRIGGER alerts_notify AFTER INSERT OR UPDATE ON alerts
    FOR EACH ROW EXECUTE FUNCTION notify_alert();
