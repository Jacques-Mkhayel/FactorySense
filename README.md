# FactorySense

Industrial IoT monitoring prototype: machines on a plant floor stream vibration, temperature and
pressure to a public cloud, which stores the telemetry, raises alerts and serves a dashboard.
Built with Docker Compose as a hybrid edge/cloud architecture.

> **Status: Phase 1 (skeleton).** Every service starts, stays healthy and is wired to the others.
> Business logic in the custom services (simulation, features, rules, API endpoints, dashboard)
> is still `TODO`, so no telemetry flows on its own yet.

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
| `ingestor` | MQTT to database, configuration only | Telegraf |
| `timescaledb` | Single database: telemetry hypertable + hourly rollup, and relational data | PostgreSQL + TimescaleDB |
| `rules-engine` | Detects anomalies and manages alerts (equipment and IDS) | Python |
| `api` | REST + WebSocket query layer, horizontally scalable | FastAPI |
| `frontend` | Dashboard | Static HTML/JS on nginx |
| `proxy` | Single public entry point, load-balances `api` replicas | Traefik |

`mqtt-certs` is a one-shot job, not a service: it creates the broker's TLS certificate on first
start and exits with code 0.

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
  telegraf/            MQTT -> database pipeline
  timescaledb/init/    telemetry hypertable, rollup, retention; app schema (TODO)
  suricata/            IDS config and local rules
  traefik/             proxy config and shared middlewares
docs/architecture.mmd  architecture diagram
```

## Limits

- **Prototype, not production.** Single host, one broker, one database: no high availability.
- **Phase 1 stubs.** The custom services only log, report health and wait at a `TODO`.
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
- **Changed a database setting and nothing happened**: `POSTGRES_*` and `TELEMETRY_*` values, and
  the TLS certificates, only apply when their volumes are first created. Apply them with
  `docker compose down -v && docker compose up -d --wait` (this wipes the data).
