# FactorySense

Industrial IoT monitoring prototype: machines on a plant floor stream vibration, temperature and
pressure to a public cloud, which stores the telemetry, raises alerts and serves a dashboard.
Built with Docker Compose as a hybrid edge/cloud architecture.

> **Status: Phase 2 (implementation).** Done: rules-engine, IDS rules, API, dashboard, proxy
> security layer and database schema. In progress: `edge-gateway` and `simulator`, so until they
> land, telemetry only flows when you publish it by hand (see "Try it").

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
| `api` | Login, alerts, telemetry and user management (REST) + live alerts (WebSocket); stateless, horizontally scalable | FastAPI, psycopg 3 |
| `frontend` | Mini SIEM dashboard: alert feed, KPIs, telemetry charts, user admin | Static HTML/CSS/JS on nginx |
| `proxy` | Single public entry point and security layer (TLS, CSP, rate limits); load-balances `api` replicas | Traefik |

`mqtt-certs` is a one-shot job, not a service: it creates the broker's TLS certificate on first
start and exits with code 0.

## Quick start

Requirements: Docker with Compose v2, and ports 80 and 443 free (see Troubleshooting otherwise).

```bash
docker compose up -d --build --wait   # .env ships with demo values; edit it for anything real
docker compose ps             # 10 services "healthy", mqtt-certs "Exited (0)"
```

Open https://localhost and accept the self-signed certificate warning (http:// redirects there).
Sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD` from `.env`; create more users in the **Users** tab.
API docs: https://localhost/api/docs.

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
for i in $(seq 9); do curl -sk https://localhost/api/health; echo; done   # "replica" changes
docker compose up -d --scale api=1
```

The dashboard shows the replica that answered last in the top bar (sessions live in the database,
so you stay signed in whichever replica answers).

**Intrusion detection**: a rogue device pings the gateway, then the gateway writes to the
read-only PLC. Both raise Suricata alerts, which appear live in the dashboard with source `ids`.

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
5. A database trigger `NOTIFY`s every alert change; each `api` replica `LISTEN`s and pushes it to
   its dashboards over a WebSocket, so the alert feed is live without polling.
6. `proxy` terminates TLS and exposes the API at `/api` and the dashboard at `/`. Analysts can
   acknowledge or resolve alerts; an acknowledged alert stays open, keeps counting repeats and
   still auto-resolves when the reading returns to normal.

**Security layers**, outside in: HTTPS only (HTTP redirects); security headers and a strict CSP
on the dashboard; per-IP rate limits (5 logins per minute), body-size and concurrency caps at the
proxy; scrypt password hashes and server-side sessions in an `HttpOnly`, `Secure`,
`SameSite=Strict` cookie (logout and account deactivation take effect at once); admin-only user
management with no public sign-up; and a least-privilege `api` database role that can read
telemetry and alerts but only change an alert's workflow columns.

Configuration lives in [`.env`](.env), committed with demo values only. No certificates or keys
are committed: they are generated on first start.

## Repository layout

```
docker-compose.yml     all services, networks, volumes and healthchecks
services/              custom code: simulator, edge-gateway, rules-engine, api, frontend
config/                configuration of the off-the-shelf components
  mosquitto/           broker config, ACL, user and certificate bootstrap
  telegraf/            MQTT -> database pipeline
  timescaledb/init/    telemetry hypertable, rollup, retention; alerts; users, sessions, api role
  suricata/            IDS config and local rules
  traefik/             proxy config and shared middlewares
docs/architecture.mmd  architecture diagram
```

## Limits

- **Prototype, not production.** Single host, one broker, one database: no high availability.
- **Work in progress.** `edge-gateway` and `simulator` are still being implemented. The rules
  engine uses fixed thresholds only: the adaptive baseline and alert escalation are TODO.
- **Security shortcuts.** Both certificates are self-signed and throwaway (MQTT CA, and Traefik's
  default HTTPS certificate, hence the browser warning and no HSTS). Only `api` has a
  least-privilege database role; `ingestor` and `rules-engine` still use the database owner.
  Traffic inside `cloud_net` (proxy to api, services to database) is not encrypted. Traefik mounts
  the Docker socket read-only to discover replicas, which is root-equivalent access.
- **Demo credentials are public.** `.env` is committed so the stack runs right after a clone; every
  `change-me` password in it is known to anyone with the repository. Replace them before exposing
  the stack, and do not commit real credentials (or untrack `.env` with `git rm --cached .env`).
- **Trusted forwarding headers.** The api trusts `X-Forwarded-For` from any peer to log client
  IPs; only Traefik reaches it, but another `cloud_net` container could spoof the logged IP.
- **Rate limits are per proxy instance** and in memory: fine for one Traefik, not shared across
  several. Behind NAT, all clients share one IP and therefore one limit.
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

- **Port 80 or 443 in use**: set `PROXY_HTTP_PORT` / `PROXY_HTTPS_PORT` in `.env`. The HTTP
  redirect always targets port 443, so with another HTTPS port open https://localhost:<port> directly.
- **Database schema changed after a pull** (e.g. `relation "users" does not exist`): init scripts
  only run on an empty volume. Recreate just the database with
  `docker compose rm -sf timescaledb && docker volume rm factorysense_timescaledb-data && docker compose up -d --wait`.
- **Build fails with `lookup registry-1.docker.io: i/o timeout`**: your active buildx builder
  cannot resolve DNS. Run `docker buildx use default` and build again.
- **Changed a database setting and nothing happened**: `POSTGRES_*` and `TELEMETRY_*` values, and
  the TLS certificates, only apply when their volumes are first created. Apply them with
  `docker compose down -v && docker compose up -d --wait` (this wipes the data).
