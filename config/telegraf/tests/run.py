"""Exercise real Telegraf + TLS Mosquitto + TimescaleDB in disposable containers.

Run: python3 config/telegraf/tests/run.py
Requires locally available project images; never changes project containers/data.
"""
import json
from pathlib import Path
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[3]
PREFIX = "factorysense-ingestor-test-" + uuid.uuid4().hex[:10]
BROKER, DB, INGESTOR = (PREFIX + suffix for suffix in ("-broker", "-db", "-ingestor"))
SERVER, CA, DATA, PGDATA = (PREFIX + suffix for suffix in ("-server", "-ca", "-mqtt-data", "-pg-data"))
PASSWORD = "isolated-test-password"
DB_PASSWORD = "test password ' with spaces"  # Driver environment must handle this literally.
VOLUMES = (SERVER, CA, DATA, PGDATA)
BASE = {"ts": time.time_ns() // 1_000_000 - 48 * 3600 * 1000, "site_id": "ingestor-test",
        "machine_id": "press-01", "temperature": -12.5, "pressure": 4.2, "vibration": 0.8}
TOPIC = "factorysense/telemetry/ingestor-test/press-01"


def docker(*args, check=True, input=None):
    result = subprocess.run(["docker", *args], input=input, text=True, capture_output=True, timeout=90)
    if check and result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result


def wait_for(predicate, message, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.5)
    raise AssertionError(message)


def sql(query):
    return docker("exec", DB, "psql", "-XAt", "-U", "factorysense", "-d", "factorysense",
                  "-v", "ON_ERROR_STOP=1", "-c", query).stdout.strip()


def logs(container):
    result = docker("logs", container, check=False)
    return result.stdout + result.stderr


def publish(payload, topic=TOPIC):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    docker("exec", "-i", BROKER, "mosquitto_pub", "-h", "mosquitto", "-p", "8883",
           "--cafile", "/certs/ca.crt", "-u", "gateway", "-P", PASSWORD,
           "-q", "1", "-t", topic, "-s", input=raw)


def rows(machine="press-01"):
    # All machine identifiers here are fixed test constants.
    return json.loads(sql("SELECT COALESCE(json_agg(t), '[]') FROM (SELECT "
                          "(extract(epoch FROM time)*1000)::bigint AS ts, site_id, machine_id, "
                          "temperature, pressure, vibration FROM telemetry "
                          f"WHERE machine_id = '{machine}' ORDER BY time) t"))


def expect_row(payload):
    wait_for(lambda: payload in rows(payload["machine_id"]), "Reading missing or changed in database")


def start_ingestor():
    env = {"MQTT_HOST": "mosquitto", "MQTT_PORT": "8883", "MQTT_USER": "ingestor",
           "MQTT_PASSWORD": PASSWORD, "MQTT_CA_FILE": "/certs/ca.crt", "PGHOST": "timescaledb",
           "PGDATABASE": "factorysense", "PGUSER": "factorysense", "PGPASSWORD": DB_PASSWORD}
    args = []
    for key, value in env.items():
        args.extend(["-e", f"{key}={value}"])
    docker("run", "-d", "--name", INGESTOR, "--network", PREFIX, *args,
           "-v", f"{ROOT / 'config/telegraf'}:/etc/telegraf:ro", "-v", f"{CA}:/certs:ro",
           "telegraf:1.40-alpine", "telegraf", "--config", "/etc/telegraf/telegraf.conf")
    wait_for(lambda: "Connected [ssl://mosquitto:8883]" in logs(INGESTOR), "Ingestor did not connect")


def main():
    try:
        docker("network", "create", "--internal", PREFIX)
        for volume in VOLUMES:
            docker("volume", "create", volume)
        docker("run", "--rm", "--network", "none", "-e", "MQTT_TLS_HOSTNAME=mosquitto",
               "-v", f"{ROOT / 'config/mosquitto/gen-certs.sh'}:/gen-certs.sh:ro",
               "-v", f"{SERVER}:/tls/server", "-v", f"{CA}:/tls/ca",
               "python:3.12-slim", "sh", "/gen-certs.sh")
        env = []
        for name in ("MQTT_GATEWAY_PASSWORD", "MQTT_INGESTOR_PASSWORD", "MQTT_RULES_PASSWORD"):
            env.extend(["-e", f"{name}={PASSWORD}"])
        docker("run", "-d", "--name", BROKER, "--network", PREFIX, "--network-alias", "mosquitto",
               *env, "-v", f"{ROOT / 'config/mosquitto'}:/mosquitto/config:ro",
               "-v", f"{SERVER}:/mosquitto/tls:ro", "-v", f"{CA}:/certs:ro",
               "-v", f"{DATA}:/mosquitto/data", "--entrypoint", "sh", "eclipse-mosquitto:2.1-alpine",
               "/mosquitto/config/entrypoint.sh", "/usr/sbin/mosquitto", "-c", "/mosquitto/config/mosquitto.conf")
        docker("run", "-d", "--name", DB, "--network", PREFIX, "--network-alias", "timescaledb",
               "-e", "POSTGRES_DB=factorysense", "-e", "POSTGRES_USER=factorysense",
               "-e", f"POSTGRES_PASSWORD={DB_PASSWORD}", "-e", "TELEMETRY_RETENTION=30 days",
               "-e", "TELEMETRY_1H_RETENTION=365 days", "-v", f"{PGDATA}:/var/lib/postgresql/data",
               "-v", f"{ROOT / 'config/timescaledb/init/010-telemetry.sh'}:/docker-entrypoint-initdb.d/010-telemetry.sh:ro",
               "timescale/timescaledb:2.30.1-pg16")
        wait_for(lambda: "PostgreSQL init process complete" in logs(DB), "Database initialization failed", 60)
        wait_for(lambda: docker("exec", DB, "pg_isready", "-U", "factorysense", check=False).returncode == 0,
                 "Database not ready")
        start_ingestor()
        publish(BASE)
        expect_row(BASE)
        assert len(rows()) == 1
        print("PASS negative temperature, exact sensor values/IDs and 48-hour-old millisecond timestamp", flush=True)

        invalid = ["not JSON", "[]", "null", dict(BASE, ts=0), dict(BASE, ts="123"),
                   dict(BASE, ts=1.5), dict(BASE, ts=9223372036855), dict(BASE, temperature="71.5"),
                   dict(BASE, temperature=True), dict(BASE, temperature=float('nan')),
                   dict(BASE, pressure=-1), dict(BASE, vibration=655.36), dict(BASE, site_id=""),
                   dict(BASE, machine_id="different-machine"), dict(BASE, temperature=None)]
        for key in BASE:
            invalid.append({name: value for name, value in BASE.items() if name != key})
        for payload in invalid:
            publish(payload)
        # A valid message after the invalid group is an ordered processing barrier.
        extra = dict(BASE, ts=BASE["ts"] + 1, temperature=90.0)
        publish(dict(extra, unexpected_sensor=999))
        expect_row(extra)
        assert rows() == [BASE, extra], rows()
        assert "unexpected_sensor" not in sql("SELECT column_name FROM information_schema.columns WHERE table_name='telemetry'")
        assert "Rejected telemetry" in logs(INGESTOR)
        assert "Error in plugin" not in logs(INGESTOR), logs(INGESTOR)
        print(f"PASS {len(invalid)} invalid messages rejected; overheating accepted; extra fields cannot alter schema", flush=True)

        docker("stop", "--time", "10", INGESTOR)
        offline = dict(BASE, ts=BASE["ts"] + 2)
        publish(offline)
        docker("start", INGESTOR)
        expect_row(offline)
        print("PASS persistent MQTT session replay after ingestor downtime", flush=True)

        docker("stop", "--time", "10", DB)
        retry = dict(BASE, ts=BASE["ts"] + 3)
        publish(retry)
        wait_for(lambda: "Error writing to outputs.postgresql" in logs(INGESTOR),
                 "Database failure was not observed by the output")
        # Crash with an uncommitted reading: the broker must retain it until the DB write.
        docker("kill", INGESTOR)
        docker("start", DB)
        wait_for(lambda: docker("exec", DB, "pg_isready", "-U", "factorysense", check=False).returncode == 0,
                 "Database failed to recover")
        docker("start", INGESTOR)
        expect_row(retry)
        print("PASS database outage + ingestor crash recovery with original timestamp", flush=True)
        health = docker("exec", INGESTOR, "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/")
        assert health.returncode == 0
        print("All isolated ingestor integration checks passed.", flush=True)
    except Exception:
        for container in (INGESTOR, DB, BROKER):
            print(f"--- {container} ---\n{logs(container)[-6000:]}")
        raise
    finally:
        for container in (INGESTOR, DB, BROKER):
            docker("rm", "-f", container, check=False)
        for volume in VOLUMES:
            docker("volume", "rm", volume, check=False)
        docker("network", "rm", PREFIX, check=False)


if __name__ == "__main__":
    main()
