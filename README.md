# FactorySense

Industrial IoT monitoring prototype: machines on a plant floor stream vibration, temperature and
pressure to a public cloud, which stores the telemetry, raises alerts and serves a dashboard.
Built with Docker Compose as a hybrid edge/cloud architecture.

> **Status: simulator, gateway, broker and ingestor implemented; cloud application still a skeleton.**
> The simulator generates normal/overheating measurements. The gateway reads them, checks
> local conditions, saves outgoing messages to disk, and replays them over MQTT/TLS.
> Mosquitto provides authenticated TLS routing and persistent subscriber queues.
> Telegraf validates telemetry and stores it in TimescaleDB with the original timestamps.
> Cloud rules, API endpoints and dashboard logic remain incomplete.

## Architecture

Diagram: [`docs/architecture.mmd`](docs/architecture.mmd). Paste the whole file into
[mermaid.live](https://mermaid.live) to view or export it.

Two Docker networks make the trust boundary real. `plant_net` is internal (no route to the cloud
or the internet); `edge-gateway` is the only container on both networks, and it listens on no port.

| Service | Role | Built with |
|---|---|---|
| `simulator` | Fake PLC exposing sensor registers over Modbus TCP | Python, pymodbus |
| `edge-gateway` | Reads the PLC, buffers up to 72h on WAN loss, publishes outbound over MQTT/TLS | Python, pymodbus |
| `ids` | Network IDS on the gateway's interfaces (shares its network namespace) | Suricata |
| `mosquitto` | MQTT broker: TLS only, one user per client, topic ACL | Eclipse Mosquitto |
| `ingestor` | Validates MQTT readings and stores them in TimescaleDB | Telegraf + Starlark |
| `timescaledb` | Single database: telemetry hypertable + hourly rollup, and relational data | PostgreSQL + TimescaleDB |
| `rules-engine` | Detects anomalies and manages alerts (equipment and IDS) | Python |
| `api` | REST + WebSocket query layer, horizontally scalable | FastAPI |
| `frontend` | Dashboard | Static HTML/JS on nginx |
| `proxy` | Single public entry point, load-balances `api` replicas | Traefik |

`mqtt-certs` is a Compose service that runs as a one-shot job: it creates the broker's TLS
certificate on first start, validates existing material on subsequent runs, and exits.

## Simulator register map

The simulator exposes the following Modbus input registers. These are the addresses the
edge gateway will use when reading measurements; TCP port `502` is the connection port,
not a register address.

| Register address | Measurement | Unit | Encoding | Resolution | Example |
|---|---|---|---|---|---|
| `0` | Temperature | °C | Multiply by 10, signed 16-bit | 0.1 °C | 71.5 → `715`; −5.2 → `65484` |
| `1` | Pressure | bar | Multiply by 100 | 0.01 bar | 4.25 → `425` |
| `2` | Vibration velocity magnitude | mm/s | Multiply by 100 | 0.01 mm/s | 0.83 → `83` |
| `3`–`15` | Reserved | — | — | — | — |

Each measurement uses one 16-bit register, transferred as a raw integer from `0` to `65535`.
The simulator multiplies by the scale and rounds to the nearest integer using Python's
`round` (ties to even). Extra decimal precision is lost during encoding.

Temperature interprets those bits as a **signed** integer from `-32768` to `32767`, using
two's complement. To encode a negative scaled integer, add `65536`: −5.2 °C becomes −52,
then raw register value `65484`. To decode temperature, subtract `65536` if the raw value
is at least `32768`, then divide by 10. Positive temperatures keep their usual encoding.
Pressure and vibration remain **unsigned**: decode them by dividing the raw value by 100.
The gateway must apply these same rules before publishing physical values as JSON.

The encoding ranges are −3276.8–3276.7 °C, 0–655.35 bar and 0–655.35 mm/s; these are storage
limits, not realistic operating ranges or alarm thresholds. Vibration represents a
velocity magnitude, not a signed waveform. Non-finite and out-of-range physical values
are rejected; negative pressure and vibration are also rejected.

`INPUT_REGISTER_MAP`, `MEASUREMENT_UNITS`, `REGISTER_SCALES` and `REGISTER_SIGNED` in
`services/simulator/src/main.py` define this contract. `encode_measurement` and
`decode_measurement` implement the conversions. The simulator fills sensor registers before
accepting connections and refreshes them approximately once per second. Reserved registers
`3`–`15` remain at zero.

## Simulator measurement generation

`generate_measurements(elapsed_s, rng=None, scenario="normal")` in `services/simulator/src/main.py` returns
one dictionary containing temperature, pressure and vibration in their physical units.
Each value is calculated as **baseline + smooth cyclic variation + bounded random noise**.
The cycle is a sine wave; its period is the time taken to complete one repetition.

| Measurement | Baseline | Cyclic variation | Period | Random noise | Possible range |
|---|---|---|---|---|---|
| Temperature | 70 °C | ±2 °C | 120 s | ±0.2 °C | 67.8–72.2 °C |
| Pressure | 4.2 bar | ±0.15 bar | 30 s | ±0.02 bar | 4.03–4.37 bar |
| Vibration velocity magnitude | 0.8 mm/s | ±0.1 mm/s | 10 s | ±0.02 mm/s | 0.68–0.92 mm/s |

These settings in `SIMULATION_PROFILES` illustrate normal operation; they are not real
machine specifications or alarm thresholds. Signed temperature encoding remains supported,
although this normal-operation profile generates positive temperatures.

`elapsed_s` means seconds since the simulation started, not a Unix timestamp. The
update loop obtains it from a monotonic clock, unaffected by wall-clock corrections. Passing a seeded `random.Random`
instance makes a sequence repeatable when called with the same elapsed times.
The generator returns unrounded physical values; `encode_measurement` handles rounding
when storing them. The optional overheating scenario is described below.

`run_simulator()` starts the Modbus server and calls `update_registers()` approximately
every `UPDATE_INTERVAL_S` seconds (currently `1.0`). Each update generates a sample,
encodes its three values, and writes addresses `0`–`2` together through the server's
`async_setValues` API. This is an internal update of input registers, not a client write.
`await asyncio.sleep(...)` allows the server to keep handling reads between updates.
The server is closed in a `finally` block when the update loop ends.

To rebuild and run the updated simulator:

```bash
docker compose up -d --build --no-deps simulator
```

With the gateway container running, read the simulator three times (raw encoded values):

```bash
docker compose exec -T edge-gateway python - <<'PY'
import time
from pymodbus.client import ModbusTcpClient

with ModbusTcpClient("simulator", port=502) as client:
    for _ in range(3):
        response = client.read_input_registers(0, count=3, device_id=1)
        if response.isError():
            raise RuntimeError(response)
        print(response.registers)  # [temperature x10, pressure x100, vibration x100]
        time.sleep(1.2)
PY
```

For negative temperatures, apply the signed decoding rule above. Small changes may
round to the same register value in consecutive samples.

## Simulator overheating scenario

Choose the simulator mode with `SIMULATION_SCENARIO` in `.env`:

- `normal` (default): use the normal measurement profiles above.
- `overheating`: add a gradual temperature rise, while pressure and vibration keep their
  normal behaviour. Any other value causes startup to fail with a configuration error.

The overheating timeline starts each time the simulator process starts:

| Elapsed time | Behaviour |
|---|---|
| 0–30 seconds | Normal readings |
| 30–90 seconds | Add 0.5 °C per second since the 30-second mark |
| 90 seconds onward | Keep the added temperature at +30 °C |

The offset is added to the usual temperature cycle and noise. At 60 seconds the offset
is +15 °C (temperature around 85 °C); after 90 seconds the temperature stays within
97.8–102.2 °C. Small sample-to-sample fluctuations can still occur. The cap limits the
extra temperature, not the total reading, and prevents an endlessly increasing value.
The timing, rate and cap are named `OVERHEATING_*` constants in the simulator code.

For a demonstration, rebuild and recreate only the simulator with the selected mode:

```bash
SIMULATION_SCENARIO=overheating docker compose up -d --build --no-deps simulator
```

Use the Modbus read example above to observe register `0` (divide its value by 10 for
these positive temperatures). Repeat reads over at least 90 seconds to see the ramp
and plateau. To return to normal operation:

```bash
SIMULATION_SCENARIO=normal docker compose up -d --no-deps simulator
```

These shell overrides select the mode for that Compose command; they do not edit `.env`.
Set `SIMULATION_SCENARIO=overheating` in `.env` instead to persist the choice. To replay the
ramp while already in overheating mode, use `docker compose restart simulator`.
A plain restart resets the timeline but does not load changed environment settings;
use `docker compose up -d --no-deps simulator` after editing `.env`.

A future rule such as "temperature > 85 °C" can use this scenario to demonstrate alert
creation. That is an illustrative threshold, not an implemented rule or an equipment
safety limit. The simulator only produces measurements; the gateway/rules engine will
perform detection later.

## Gateway collection and connection recovery

The gateway reads input registers `0`–`2` from `simulator:502`, device ID `1`. It decodes
temperature as a signed 16-bit integer divided by 10, and pressure/vibration as unsigned
integers divided by 100. For example `[65484, 425, 83]` means −5.2 °C, 4.25 bar, 0.83 mm/s.
Invalid or incomplete responses produce no reading; previous data or zeros are not substituted.

`POLL_INTERVAL_S` defaults to 1 second. After a failed Modbus attempt, retries wait 1, 2,
4, … seconds up to 30 seconds (starting at the polling interval if it exceeds 1 second).
Successful reads reset the retry delay. Each network attempt has a 3-second timeout.
MQTT reconnects independently with increasing delays capped at 30 seconds. A PLC outage
does not stop replaying saved messages; a broker outage does not stop collecting readings
or evaluating local checks. Measurements cannot be recovered for times when the PLC itself
was unavailable.

The loopback `/health` endpoint returns connection flags, pending-message count,
`last_sample_ts`, local alarm state and worker errors. During PLC/broker outages it returns
HTTP 200 with `status=degraded`: the gateway is alive and attempting recovery. `status=ok`
means both connections are up, not that every downstream service processed the data.
An unrecoverable replay-worker error is exposed and makes the gateway exit for Docker to
restart it. SQLite write failures also terminate the process instead of silently dropping
new data. Pending committed data is retained. SIGTERM/SIGINT stop polling and replay and
close SQLite and network clients cleanly.

## Gateway telemetry and secure MQTT

Each successful read produces a JSON message such as:

```json
{
  "ts": 1790000000000,
  "site_id": "plant-01",
  "machine_id": "press-01",
  "temperature": -5.2,
  "pressure": 4.25,
  "vibration": 0.83
}
```

`ts` is gateway collection time in Unix milliseconds, captured immediately after the
Modbus read. It is not the PLC's generation time: the register map contains no timestamp.
The JSON and its original `ts` are saved unchanged and replayed unchanged. `SITE_ID` and
`MACHINE_ID` come from Compose/`.env` (defaults `plant-01` / `press-01`). They must be
nonempty and cannot contain `/`, `+`, `#` or null characters.

The telemetry topic is `factorysense/telemetry/<site>/<machine>`. MQTT uses QoS 1 and
`retain=false`. `paho-mqtt==2.1.0` handles the network on a background thread. The configured
CA (`MQTT_CA_FILE=/certs/ca.crt`) must validate the broker certificate and its hostname
(`MQTT_HOST=mosquitto`). TLS 1.2 is the minimum version; `MQTT_PORT=8883` is the default.
`MQTT_USER=gateway` and `MQTT_PASSWORD` (from `.env`'s `MQTT_GATEWAY_PASSWORD`) authenticate
the client. The password is never included in the logged configuration. Missing credentials
or invalid CA configuration fail startup; there is no insecure fallback.

The client ID is `factorysense-gateway-<site>-<machine>`: use one running gateway per
site/machine pair, each with its own buffer. The health endpoint is only on loopback;
the gateway exposes no application listener to either Docker network.

## Persistent buffer and replay

Every sample is written to SQLite **before publishing**, even when MQTT is connected.
`BUFFER_PATH=/var/lib/gateway/buffer.db` is stored in the existing `gateway-buffer` Docker
volume. SQLite transactions, WAL and full synchronization keep committed samples across
process/container restarts. Do not delete that volume if you want to preserve pending data.
A single gateway process owns a buffer; multiple replicas sharing it are not supported.

An independent replay worker sends the oldest pending message first, one at a time. It
removes that specific row only after a matching QoS 1 broker acknowledgement. Callback
acknowledgements are passed through a thread-safe queue, so even an acknowledgement that
arrives before `publish()` returns is handled. Paho's in-memory queue is limited to one
message; SQLite owns the backlog.

If an acknowledgement is missing for `MQTT_ACK_TIMEOUT_S` (default 30 seconds), the worker
retires the old MQTT client and its memory queue, creates a fresh client, and retries the
saved row. Fresh acknowledgement queues prevent old message IDs deleting a different row.
Collection continues independently during replay, reconnection and acknowledgement waits.

Pending messages older than `GATEWAY_BUFFER_RETENTION_H` (default 72 hours) are deleted,
with a `buffer_expired` warning containing the count. Retention is based on original
collection time and is enforced even while MQTT is down. This intentionally bounds the
age of pending data; disk capacity must still accommodate the configured sampling rate.
SQLite reuses freed space; deleting rows need not immediately shrink the file.

Delivery is **at least once**, not exactly once. A crash after the broker receives a message
but before SQLite records its acknowledgement can produce a duplicate on replay. The
original payload and timestamp remain identical. The current telemetry table does not
deduplicate these automatically. A PUBACK confirms the broker handshake, not a database
insert, and MQTT 3.1.1 cannot report every topic-ACL rejection through that acknowledgement.

Useful logs:

- `telemetry_buffered`: the reading has been committed locally.
- `mqtt_connected` / `mqtt_connection_failed` / `mqtt_connection_rejected`: connection state.
- `mqtt_publish_queued`: a durable row has been submitted to Paho.
- `mqtt_publish_acknowledged`: the broker handshake completed.
- `buffer_delivered`: the matching durable row was deleted after acknowledgement.
- `mqtt_delivery_retry`: the acknowledgement deadline expired or the pending row expired.
- `buffer_expired`: retention intentionally removed old pending messages.
- `modbus_read_failed` / `modbus_recovered`: PLC connection recovery.

## Local calculations and overheating detection

`local_checks.py` processes fresh samples without needing the broker or database. Over a
rolling `LOCAL_WINDOW_S` window (default 60 seconds), it calculates the sample-based mean
temperature, mean pressure, and maximum vibration magnitude. These are logged as
`local_features`; they are not additional telemetry columns. The window holds at most
10,000 samples, resets after process restart, and excludes expired samples after a PLC
outage. It is not a time-weighted average or vibration waveform/RMS calculation.

The first local rule is configurable demo overheating:

- Trigger immediately when the current temperature is **at least 85 °C**
  (`LOCAL_TEMP_CRITICAL_C`), without waiting for a rolling average.
- Remain active until a fresh reading is **below 80 °C** (`LOCAL_TEMP_CLEAR_C`).
- Emit one activation and one resolution event per episode, rather than repeated alarms
  on every hot sample. The separate clear threshold prevents repeated toggling near 85 °C.
- Persist the alarm state with its transition event in the same transaction. A restart
  while hot does not emit a duplicate activation. Missing readings do not clear an alarm;
  consult `last_sample_ts` and `modbus_connected` to distinguish fresh and stale state.

The thresholds are illustrative, not real equipment safety limits. They are not used to
control or shut down the PLC. Pressure and vibration are calculated/reported but currently
have no local alarm thresholds.

Transitions are logged immediately as `local_alert` and buffered for QoS 1 publication to
`factorysense/status/<site>/<machine>`, an already-allowed gateway topic. The event includes
its original timestamp, source IDs, `rule=overheating`, `state=active|resolved`, severity,
temperature, thresholds and recent features. Alert state and queued events survive outages
and restarts. Status events use the same retention policy as telemetry.

Telegraf subscribes to telemetry only. The broker now permits the rules-engine account to
read status events, but its application still needs to subscribe and process them: **these local
status events are not yet stored as cloud alerts or displayed by the dashboard**. Cloud
alert processing remains separate work. The raw temperature still reaches telemetry so
future cloud rules can detect overheating independently.

## Running and verifying the gateway

The code is split into `main.py` (collection, MQTT setup and health), `outbox.py` (durable
storage), `publisher.py` (replay worker), and `local_checks.py` (calculations/rules).
New settings are documented in `.env.example`; Compose supplies defaults for existing
`.env` files. Rebuild the services, including `ids` because it shares the gateway's network
namespace:

```bash
docker compose up -d --build --wait simulator mosquitto edge-gateway ids
docker compose logs --tail=30 -f edge-gateway
```

Inspect health, including the number of pending telemetry and status messages:

```bash
docker compose exec edge-gateway python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').read().decode())"
```

For an outage demonstration, stop Mosquitto, let the gateway collect readings, inspect the
pending count, then start Mosquitto again. Restarting the gateway while offline also keeps
pending data. Recreating the gateway requires recreating `ids` with it.

```bash
docker compose stop mosquitto
# Wait for several readings; inspect gateway logs/health.
docker compose start mosquitto
# Pending count falls as the broker acknowledges replayed messages.
```

Use the simulator's overheating mode to exercise local alerts even while MQTT is down.
For storage verification, allow Telegraf its 5-second flush interval, then use your configured
database credentials (the following uses the Compose-provided ones inside the container):

```bash
docker compose exec timescaledb sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT time, site_id, machine_id, temperature FROM telemetry ORDER BY time DESC LIMIT 10;"'
```

Run the automated regression suite with the gateway's dependencies:

```bash
docker compose run --rm --no-deps -T -v "$PWD/services/edge-gateway:/work/gateway:ro" --entrypoint python edge-gateway -m unittest discover -s /work/gateway/tests -v
```

`services/edge-gateway/tests/integration_replay.py` is an explicit integration test for the
actual process, SQLite crash recovery, TLS/MQTT replay, and local alarm recovery. It uses
a temporary buffer/test PLC and uniquely labelled `replay-test-*` messages. It requires
the ingestor password in `MQTT_TEST_SUBSCRIBER_PASSWORD` for its temporary subscriber.

## Mosquitto broker

Mosquitto is the running MQTT service supplied by the `eclipse-mosquitto:2.1-alpine` image.
There is no custom broker `main.py`. The gateway sends messages to a **topic** (a routing
address), and Mosquitto forwards them to clients allowed to subscribe to that topic.
For example, a reading on `factorysense/telemetry/plant-01/press-01` reaches Telegraf.
The broker forwards the payload unchanged, including the original `ts`; it does not decode
registers, calculate temperatures, or evaluate the overheating threshold.

The implementation is configuration plus two startup scripts:

| File | Responsibility |
|---|---|
| `docker-compose.yml` | Runs the broker, attaches `cloud_net`, mounts certificates/data, supplies credentials and checks health. |
| `config/mosquitto/mosquitto.conf` | TLS listener, password/ACL paths, persistence and logs. |
| `config/mosquitto/acl` | Defines which topics each account may publish to or read. |
| `config/mosquitto/entrypoint.sh` | Requires all three passwords, builds a hashed password file, protects runtime files, then starts Mosquitto. |
| `config/mosquitto/gen-certs.sh` | Creates the prototype CA/server certificate or validates an existing complete set. |

### Secure connections and permissions

Clients use TLS on **8883** and verify the broker's certificate against the shared CA and
its DNS name, `mosquitto`. They then authenticate with their own username/password from
`.env`. This is server-certificate TLS with password authentication; clients do not need
individual certificates. The broker has no host-published port or network plaintext MQTT
listener. Its anonymous **1880** listener binds only to container loopback for the uptime
healthcheck; anonymous access permits reading `$SYS/broker/uptime` only. That healthcheck
checks local broker liveness; the integration tests below separately verify TLS/authentication.

| Account | May publish | May read |
|---|---|---|
| `gateway` | `factorysense/telemetry/#`, `factorysense/status/#` | Nothing |
| `ingestor` | Nothing | `factorysense/telemetry/#` |
| `rules-engine` | Nothing | `factorysense/telemetry/#`, `factorysense/status/#` |

`#` means all topic levels below that prefix. Other operations are denied. Accounts are
shared service roles in this prototype, not separate identities per machine. Permission to
read status prepares the rules engine to receive gateway overheating transitions; cloud
alert processing is still unfinished.

Startup rejects missing/empty passwords. Runtime key/hash files use mode `0600`, inside a
`0700` directory owned by Mosquitto. Certificate bootstrap refuses incomplete material,
a certificate that fails CA/expiry/hostname verification, or a mismatched private key,
instead of silently replacing a CA that existing clients may still trust. The prototype
certificate lifetime is one year; renewal is manual.

### Queues and restart recovery

Broker state is saved in the `mosquitto-data` volume every **30 seconds** and on clean
shutdown. This preserves subscriptions and queued QoS 1/2 messages for persistent sessions,
and retained messages if clients use them. Telegraf already requests a persistent session
with a stable client ID. A subscriber must first connect and establish its subscription;
Mosquitto does not reconstruct messages sent before that subscription existed.

There are three different storage responsibilities:

- **Gateway SQLite buffer:** readings waiting to be acknowledged by the broker, including
  while the broker/network is unavailable.
- **Broker persistence:** MQTT session/queued-message state, including while an established
  persistent subscriber is offline. Queue limits still apply (Mosquitto's default is 1,000
  queued QoS 1/2 messages per client).
- **TimescaleDB:** measurement history after ingestion.

A broker acknowledgement is not confirmation of a database insert. An abrupt broker crash
can lose changes since the last persistence save; QoS 1 can also produce duplicates. The
30-second checkpoint is not a guarantee of lossless delivery or unlimited offline storage.
See the [official Mosquitto configuration reference](https://mosquitto.org/man/mosquitto-conf-5.html)
for persistence, queue limits and ACL behavior.

### Verify the broker

With Docker running and the gateway image built (it supplies the test client's Python/Paho):

```bash
docker compose build edge-gateway
python3 config/mosquitto/tests/run.py
```

The test creates a separate broker, internal network, certificate/data volumes and disposable
credentials, then removes them. It checks certificate creation/reuse and invalid material,
missing credentials, runtime file permissions, TLS verification, authentication, all three
roles' allowed/denied topic operations, unchanged JSON delivery, and a persistent subscriber's
queued message across a clean broker restart. Existing project containers/data are untouched.
It does not test sudden power loss or full database/dashboard delivery.

To apply the configuration to an existing stack, rerun certificate validation, then recreate
the broker so it regenerates its password/ACL runtime files and loads the updated settings:

```bash
docker compose run --rm --no-deps mqtt-certs
docker compose up -d --no-deps --force-recreate --wait mosquitto
docker compose logs --tail=30 mosquitto
```

This briefly interrupts broker connections; the implemented gateway reconnects and replays
its buffer. The named volumes are preserved. Changing `.env` passwords also requires
recreating the affected clients so their credentials match.

## Ingestor: MQTT readings into TimescaleDB

The `ingestor` service runs Telegraf from its Docker image. Its configuration is
`config/telegraf/telegraf.conf`; a small script, `config/telegraf/telemetry.star`, runs inside
Telegraf to validate and map each message. Starlark resembles Python, but it is not a
separate Python service. The path is:

```text
Mosquitto telemetry topic -> Telegraf validation -> telemetry table in TimescaleDB
```

### Message contract and database mapping

The input subscribes to `factorysense/telemetry/#` using the ingestor's MQTT credentials,
verified TLS, QoS 1 and the stable persistent client ID `factorysense-ingestor`. There must
be only one active instance with that ID. Status/overheating events are handled separately
by the future rules engine; they are not measurement rows.

Each message must be a JSON object with all six fields:

| JSON field | Database column | Meaning |
|---|---|---|
| `ts` | `time` (`timestamptz`) | Original gateway collection time, Unix milliseconds |
| `site_id` | `site_id` (`text`) | Plant identifier |
| `machine_id` | `machine_id` (`text`) | Machine identifier |
| `temperature` | `temperature` (`double precision`) | Degrees Celsius, including negative temperatures |
| `pressure` | `pressure` (`double precision`) | Bar |
| `vibration` | `vibration` (`double precision`) | Millimetres per second |

Validation requires nonempty string identifiers that match the MQTT topic exactly. The
sensor values must be JSON numbers, not strings, booleans, nulls or arrays. They must fit
the agreed register encoding: temperature -3276.8..3276.7, pressure/vibration 0..655.35.
These are encoding limits, not alarm thresholds: a 90 °C overheating reading is accepted.
`ts` must be a positive integer that fits Telegraf's nanosecond timestamp representation;
missing/invalid timestamps are never replaced with arrival time. Replayed old readings
keep their original time. Existing database retention still applies to backfilled data.

Invalid messages are logged as `Rejected telemetry` and deliberately dropped/acknowledged,
so they do not repeatedly block valid readings. They are not stored in a quarantine table.
Extra JSON keys are ignored. The processor keeps only the three sensor fields and two ID
tags, then converts milliseconds to Telegraf nanoseconds. It modifies the existing metric
so MQTT delivery tracking is preserved.

The PostgreSQL output targets the existing `public.telemetry` hypertable. Automatic table
and column creation are disabled; schema changes belong in database initialization or an
explicit migration. Existing data requires no migration for this implementation.

### Delivery and recovery

Telegraf flushes a batch every 5 seconds, or when 100 readings accumulate. The output
buffer holds up to 10,000 metrics in memory, while MQTT limits delivery to 500 pending
messages. Failed database writes are retried. The MQTT input tracks delivery through the
outputs before acknowledging accepted readings; a persistent broker session can therefore
redeliver unacknowledged readings following an ingestor interruption. Invalid messages
are intentionally acknowledged after rejection. MQTT reconnect delay is capped at 30 seconds.
See the [Telegraf MQTT input documentation](https://github.com/influxdata/telegraf/tree/v1.40.0/plugins/inputs/mqtt_consumer).

Delivery is **at least once**, not exactly once: retries may create duplicate rows. This
implementation does not deduplicate them. Broker queue limits and persistence checkpoints
still apply; the Telegraf memory buffer is not a separate durable disk queue. The local
HTTP endpoint on `127.0.0.1:8080` reports process liveness, not database connectivity or
proof that recent readings were stored.

Compose passes `PGHOST`, `PGDATABASE`, `PGUSER` and `PGPASSWORD` directly to the PostgreSQL
driver, so passwords with spaces/quotes do not need connection-string escaping. The
prototype still uses the configured database owner and unencrypted database traffic on
`cloud_net`; a dedicated restricted database role remains future hardening work. MQTT
continues to use the separate read-only ingestor account and verified TLS.

### Run and verify ingestion

Apply the service configuration without rebuilding an image:

```bash
docker compose up -d --no-deps --force-recreate --wait ingestor
docker compose logs --tail=30 -f ingestor
```

Verify actual inserts (allow at least one 5-second flush):

```bash
docker compose exec timescaledb sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT time, site_id, machine_id, temperature, pressure, vibration FROM telemetry ORDER BY time DESC LIMIT 10;"'
```

Run repeatable integration checks:

```bash
python3 config/telegraf/tests/run.py
```

The test runs separate Mosquitto, Telegraf and TimescaleDB containers on an isolated network
with disposable credentials/volumes. It checks exact values and a timestamp from 48 hours
earlier, 21 malformed messages, extra-field filtering, permitted overheating data, ingestion
after an offline subscription, and recovery after a database outage plus an ingestor crash.
It also tests a database password containing spaces and a quote. All temporary containers,
volumes and the network are removed; existing project services/data are untouched.

## Quick start

Requirements: Docker with Compose v2, and port 80 free (or set `PROXY_HTTP_PORT` in `.env`).

```bash
cp .env.example .env          # then replace the change-me values
docker compose up -d --build --wait
docker compose ps             # 10 services "healthy", mqtt-certs "Exited (0)"
```

Open http://localhost: the dashboard should show **API status: ok**.
API docs: http://localhost/api/docs.

Stop with `docker compose down` (keeps data) or `docker compose down -v` (full reset, also
regenerates certificates and re-runs database init).

## Try it

Run these from the repository root with the stack up.

**Send a reading through the whole pipeline** (MQTT/TLS → Telegraf → TimescaleDB):

```bash
source .env
docker run --rm --network factorysense_cloud_net -v factorysense_mqtt-tls-ca:/certs:ro \
  eclipse-mosquitto:2.1-alpine mosquitto_pub -h mosquitto -p 8883 --cafile /certs/ca.crt \
  -u gateway -P "$MQTT_GATEWAY_PASSWORD" -q 1 -t factorysense/telemetry/plant-01/press-01 \
  -m "{\"ts\":$(date +%s000),\"site_id\":\"plant-01\",\"machine_id\":\"press-01\",\"temperature\":71.5}"
sleep 5   # Telegraf writes every 5 s
docker compose exec timescaledb psql -U factorysense -c "SELECT * FROM telemetry ORDER BY time DESC LIMIT 5;"
```

**Network isolation**: the PLC cannot reach the cloud (fails with a name resolution error).

```bash
docker compose exec simulator python -c "import socket; socket.create_connection(('mosquitto', 8883), timeout=2)"
```

**Horizontal scaling**: Traefik spreads requests across three API replicas.

```bash
docker compose up -d --scale api=3 --wait
sleep 5   # give Traefik time to register the new replicas
for i in $(seq 9); do curl -s http://localhost/api/health > /dev/null; done
docker compose logs --since 30s proxy | grep -o 'http://[0-9.]*:8000' | sort | uniq -c
docker compose up -d --scale api=1
```

**Intrusion detection**: a rogue device pings the gateway, then the gateway writes to the
read-only PLC. Both raise Suricata alerts.

```bash
docker run --rm --network factorysense_plant_net alpine:3.24 ping -c 2 edge-gateway
docker compose exec edge-gateway python -c "from pymodbus.client import ModbusTcpClient as C; c = C('simulator', port=502); c.connect(); c.write_register(0, 1)"
docker compose exec ids grep -o '"signature":"[^"]*"' /var/log/suricata/eve.json
```

**WAN outage**: the gateway stays up while the broker is down.

```bash
docker compose stop mosquitto
docker compose ps edge-gateway   # still healthy
docker compose start mosquitto
```

## How it works

1. `simulator` holds sensor values in Modbus input registers; `edge-gateway` reads them.
2. The gateway publishes JSON readings to `factorysense/telemetry/<site>/<machine>` over TLS on
   port 8883. The timestamp travels in the payload (`ts`), so data buffered during an outage
   keeps its original time when it is sent later.
3. `ingestor` writes each reading into the `telemetry` hypertable. TimescaleDB rolls it up hourly
   into `telemetry_1h` and drops raw data after 30 days (rollups after 365).
4. `rules-engine` evaluates the same stream, plus Suricata's `eve.json`, and stores alerts with a
   `source` of `equipment` or `ids`.
5. `api` reads the database; `proxy` exposes it at `/api` and the dashboard at `/`.

Configuration lives in `.env` (see comments in [`.env.example`](.env.example)).
No secrets or certificates are committed.

## Repository layout

```
docker-compose.yml     all services, networks, volumes and healthchecks
services/              custom code: simulator, edge-gateway, rules-engine, api, frontend
config/                configuration of the off-the-shelf components
  mosquitto/           broker config, ACL, user and certificate bootstrap
  telegraf/            MQTT -> validation -> database pipeline and integration tests
  timescaledb/init/    telemetry hypertable, rollup, retention; app schema (TODO)
  suricata/            IDS config and local rules
  traefik/             proxy config and shared middlewares
docs/architecture.mmd  architecture diagram
```

## Limits

- **Prototype, not production.** Single host, one broker, one database: no high availability.
- **Incomplete cloud application.** Simulator and gateway collection, durable replay and
  local overheating checks are implemented. Cloud rules, API queries and dashboard logic
  still need implementation; local status events are not yet integrated into cloud alerts.
- **Security shortcuts.** The TLS CA is self-signed and throwaway; services use the database
  owner account; only MQTT is encrypted (HTTP and database traffic inside `cloud_net` are not);
  Traefik mounts the Docker socket read-only to discover replicas, which is root-equivalent access.
- **IDS visibility.** Suricata only sees traffic on the gateway, and not MQTT topics or payloads
  because of TLS. Unauthorised topics are silently dropped by the broker ACL instead.
- **The PLC accepts Modbus writes**, like most real PLCs. Read-only is the gateway's policy;
  writes are detected, not prevented.
- **No push notifications** (email, SMS, chat): future work.
- **Licence.** TimescaleDB Community edition is free but under the Timescale License, not an
  OSI-approved licence.
- **Fixed subnets** `172.28.10.0/24` and `172.28.20.0/24`; change them in `docker-compose.yml` and
  `config/suricata/suricata.yaml` if they clash with your network.
- Tested on Linux (Docker 29, Compose 5.5).

## Troubleshooting

- **Port 80 in use**: set `PROXY_HTTP_PORT=8080` in `.env` and use http://localhost:8080.
- **Build fails with `lookup registry-1.docker.io: i/o timeout`**: your active buildx builder
  cannot resolve DNS. Run `docker buildx use default` and build again.
- **Changed a database setting and nothing happened**: database initialization settings only
  apply when the database volume is first created. Update an existing database explicitly,
  or reset its volume only if you intend to discard its data.
- **Certificate bootstrap refuses to start**: check its logs for missing material, expiry,
  hostname mismatch or a mismatched key. Restore the matching certificate/key/CA set, or
  deliberately rotate both `mqtt-tls-server` and `mqtt-tls-ca` volumes together while the
  MQTT services are stopped, then recreate the services. Clients must reload the new CA.
  Do not remove the database or gateway-buffer volumes to repair certificates.
