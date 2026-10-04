#!/bin/sh
# Runs once, on the first start with an empty timescaledb-data volume.
# Telemetry storage only: this table is the payload contract with Telegraf (tags -> text columns,
# fields -> double precision columns). Telegraf validates and writes only this fixed schema.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v raw_retention="$TELEMETRY_RETENTION" \
     -v rollup_retention="$TELEMETRY_1H_RETENTION" <<'SQL'
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE telemetry (
    time        timestamptz      NOT NULL,
    site_id     text             NOT NULL,
    machine_id  text             NOT NULL,
    vibration   double precision,
    temperature double precision,
    pressure    double precision
);
SELECT create_hypertable('telemetry', by_range('time', INTERVAL '1 day'));
CREATE INDEX ON telemetry (machine_id, time DESC);

-- Hourly rollup for long-term trends; replaces a separate downsampling job.
CREATE MATERIALIZED VIEW telemetry_1h WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 hour', time) AS bucket,
       site_id,
       machine_id,
       avg(vibration)   AS vibration_avg,   max(vibration)   AS vibration_max,
       avg(temperature) AS temperature_avg, max(temperature) AS temperature_max,
       avg(pressure)    AS pressure_avg,    max(pressure)    AS pressure_max
FROM telemetry
GROUP BY bucket, site_id, machine_id
WITH NO DATA;

-- Re-aggregates the last 3 days each hour, so data backfilled after a WAN outage
-- (the gateway buffers up to 72h) still lands in the rollup.
SELECT add_continuous_aggregate_policy('telemetry_1h',
    start_offset      => INTERVAL '3 days',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour');

SELECT add_retention_policy('telemetry',    INTERVAL :'raw_retention');
SELECT add_retention_policy('telemetry_1h', INTERVAL :'rollup_retention');
SQL
