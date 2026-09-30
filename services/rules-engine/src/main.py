"""Rules engine: detects equipment anomalies and manages Suricata IDS alerts."""
import json
import logging
import os
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt
import psycopg2
from psycopg2.extras import Json
from psycopg2.pool import ThreadedConnectionPool

SERVICE = os.getenv("SERVICE_NAME", "rules-engine")
PORT = int(os.getenv("PORT", "8000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# MQTT Configuration
MQTT_HOST = os.getenv("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USER = os.getenv("MQTT_USER", "rules-engine")
MQTT_PASSWORD = os.getenv("MQTT_RULES_PASSWORD", "")
MQTT_CA_FILE = os.getenv("MQTT_CA_FILE", "/certs/ca.crt")

# Database Configuration
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "timescaledb")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.getenv("POSTGRES_DB", "factorysense")
POSTGRES_USER = os.getenv("POSTGRES_USER", "factorysense")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")

# IDS eve.json path
IDS_EVE_PATH = os.getenv("IDS_EVE_PATH", "/var/log/suricata/eve.json")

# Calibrated Thresholds
THRESHOLDS = {
    "temperature": {
        "warning": float(os.getenv("TEMP_WARN_THRESHOLD", "75.0")),
        "critical": float(os.getenv("TEMP_CRIT_THRESHOLD", "85.0")),
    },
    "vibration": {
        "warning": float(os.getenv("VIB_WARN_THRESHOLD", "4.0")),
        "critical": float(os.getenv("VIB_CRIT_THRESHOLD", "6.0")),
    },
    "pressure": {
        "min_critical": float(os.getenv("PRESS_MIN_CRIT", "4.0")),
        "max_critical": float(os.getenv("PRESS_MAX_CRIT", "8.0")),
    },
}
SENSOR_TIMEOUT_S = float(os.getenv("SENSOR_TIMEOUT_S", "10.0"))
DEDUPE_WINDOW_S = float(os.getenv("DEDUPE_WINDOW_S", "30.0"))

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s level=%(levelname)s service=%(name)s msg=%(message)s"
)
log = logging.getLogger(SERVICE)

# Connection pool & thread synchronization
db_pool: ThreadedConnectionPool | None = None
last_seen_machines: dict[str, dict] = {}
lock = threading.Lock()


# ----------------------------------------------------------------------
# 1. Healthcheck HTTP Server
# ----------------------------------------------------------------------
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = (200, {"status": "ok"}) if self.path == "/health" else (404, {"error": "not found"})
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)


# ----------------------------------------------------------------------
# 2. Database Connection Pool & Operations
# ----------------------------------------------------------------------
def init_db_pool():
    global db_pool
    while db_pool is None:
        try:
            db_pool = ThreadedConnectionPool(
                minconn=2,
                maxconn=10,
                host=POSTGRES_HOST,
                port=POSTGRES_PORT,
                dbname=POSTGRES_DB,
                user=POSTGRES_USER,
                password=POSTGRES_PASSWORD,
                connect_timeout=5,
            )
            log.info("Database connection pool initialized")
        except Exception as e:
            log.warning("Database not ready yet (%s). Retrying in 3s...", e)
            time.sleep(3)

    # Ensure alerts table exists
    conn = db_pool.getconn()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""
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
            """)
        log.info("Alerts table ready in database")
    finally:
        db_pool.putconn(conn)


def save_or_dedupe_alert(source: str, site_id: str | None, machine_id: str | None,
                         rule_name: str, severity: str, details: dict, dedupe_key: str):
    """Upsert alert: increment count if active within dedupe window, else insert new alert."""
    conn = None
    try:
        conn = db_pool.getconn()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE alerts
                SET count = count + 1,
                    last_seen = NOW(),
                    severity = %s,
                    rule_name = %s,
                    details = %s
                WHERE dedupe_key = %s
                  AND status = 'active'
                  AND last_seen >= NOW() - (%s * INTERVAL '1 second')
                RETURNING id, count;
            """, (severity, rule_name, Json(details), dedupe_key, DEDUPE_WINDOW_S))
            row = cur.fetchone()

            if row:
                alert_id, count = row
                log.info("Updated active alert [%s] id=%d count=%d rule='%s' severity=%s",
                         source, alert_id, count, rule_name, severity)
            else:
                cur.execute("""
                    INSERT INTO alerts (time, source, site_id, machine_id, rule_name, severity, status, details, dedupe_key, count, last_seen)
                    VALUES (NOW(), %s, %s, %s, %s, %s, 'active', %s, %s, 1, NOW())
                    RETURNING id;
                """, (source, site_id, machine_id, rule_name, severity, Json(details), dedupe_key))
                alert_id = cur.fetchone()[0]
                log.warning("NEW ALERT [%s] id=%d severity=%s rule='%s' site=%s machine=%s",
                            source, alert_id, severity, rule_name, site_id, machine_id)
    except Exception as e:
        log.error("Failed to save alert: %s", e)
    finally:
        if conn:
            db_pool.putconn(conn)


def resolve_alert(dedupe_key: str):
    """Marks an active alert as resolved when telemetry returns to normal."""
    conn = None
    try:
        conn = db_pool.getconn()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE alerts
                SET status = 'resolved', last_seen = NOW()
                WHERE dedupe_key = %s AND status = 'active'
                RETURNING id, rule_name;
            """, (dedupe_key,))
            row = cur.fetchone()
            if row:
                log.info("RESOLVED alert id=%d rule='%s' (metric returned to normal)", row[0], row[1])
    except Exception as e:
        log.error("Failed to resolve alert: %s", e)
    finally:
        if conn:
            db_pool.putconn(conn)


# ----------------------------------------------------------------------
# 3. IDS Suricata Worker (Tail eve.json)
# ----------------------------------------------------------------------
def ids_worker():
    """Tails /var/log/suricata/eve.json with log rotation handling."""
    log.info("IDS worker starting, waiting for %s...", IDS_EVE_PATH)
    while not os.path.exists(IDS_EVE_PATH):
        time.sleep(2)

    current_inode = None
    f = None

    while True:
        try:
            if not os.path.exists(IDS_EVE_PATH):
                time.sleep(1)
                continue

            inode = os.stat(IDS_EVE_PATH).st_ino
            if inode != current_inode:
                if f:
                    f.close()
                f = open(IDS_EVE_PATH, "r", encoding="utf-8")
                f.seek(0, os.SEEK_END)
                current_inode = inode
                log.info("Opened/re-opened IDS log file %s", IDS_EVE_PATH)

            line = f.readline()
            if not line:
                time.sleep(0.5)
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("event_type") != "alert":
                continue

            alert_info = event.get("alert", {})
            signature = alert_info.get("signature", "Unknown IDS alert")
            sig_id = alert_info.get("signature_id", 0)
            suricata_sev = alert_info.get("severity", 3)
            severity_map = {1: "critical", 2: "warning", 3: "info"}
            severity = severity_map.get(suricata_sev, "warning")

            src_ip = event.get("src_ip", "unknown")
            dest_ip = event.get("dest_ip", "unknown")
            dest_port = event.get("dest_port")

            dedupe_key = f"ids:{sig_id}:{src_ip}->{dest_ip}"
            details = {
                "signature_id": sig_id,
                "category": alert_info.get("category"),
                "src_ip": src_ip,
                "src_port": event.get("src_port"),
                "dest_ip": dest_ip,
                "dest_port": dest_port,
                "proto": event.get("proto"),
            }

            save_or_dedupe_alert(
                source="ids",
                site_id="plant-01",
                machine_id=None,
                rule_name=signature,
                severity=severity,
                details=details,
                dedupe_key=dedupe_key,
            )
        except Exception as e:
            log.error("Error in IDS worker: %s", e)
            time.sleep(1)


# ----------------------------------------------------------------------
# 4. MQTT Telemetry Worker (Equipment Rules + Auto-resolution)
# ----------------------------------------------------------------------
def on_mqtt_connect(client, _userdata, _flags, rc, _properties=None):
    if rc == 0:
        log.info("Connected to MQTT broker (Mosquitto TLS). Subscribing...")
        client.subscribe("factorysense/telemetry/#", qos=1)
    else:
        log.error("MQTT connection failed with code %d", rc)


def on_mqtt_message(_client, _userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception:
        return

    site_id = payload.get("site_id", "unknown")
    machine_id = payload.get("machine_id", "unknown")
    temp = payload.get("temperature")
    vib = payload.get("vibration")
    press = payload.get("pressure")

    with lock:
        last_seen_machines[machine_id] = {
            "site_id": site_id,
            "last_seen": time.time(),
        }

    # If sensor was silent, resolve the sensor loss alert
    resolve_alert(f"equipment:{site_id}:{machine_id}:sensor_loss")

    # --- 1. Temperature Check ---
    temp_key = f"equipment:{site_id}:{machine_id}:temp"
    if temp is not None:
        if temp >= THRESHOLDS["temperature"]["critical"]:
            save_or_dedupe_alert("equipment", site_id, machine_id, "Critical High Temperature", "critical",
                                 {"metric": "temperature", "value": temp, "unit": "°C"}, temp_key)
        elif temp >= THRESHOLDS["temperature"]["warning"]:
            save_or_dedupe_alert("equipment", site_id, machine_id, "High Temperature Warning", "warning",
                                 {"metric": "temperature", "value": temp, "unit": "°C"}, temp_key)
        else:
            resolve_alert(temp_key)

    # --- 2. Vibration Check ---
    vib_key = f"equipment:{site_id}:{machine_id}:vib"
    if vib is not None:
        if vib >= THRESHOLDS["vibration"]["critical"]:
            save_or_dedupe_alert("equipment", site_id, machine_id, "Critical High Vibration", "critical",
                                 {"metric": "vibration", "value": vib, "unit": "mm/s"}, vib_key)
        elif vib >= THRESHOLDS["vibration"]["warning"]:
            save_or_dedupe_alert("equipment", site_id, machine_id, "High Vibration Warning", "warning",
                                 {"metric": "vibration", "value": vib, "unit": "mm/s"}, vib_key)
        else:
            resolve_alert(vib_key)

    # --- 3. Pressure Check ---
    press_key = f"equipment:{site_id}:{machine_id}:press"
    if press is not None:
        if press <= THRESHOLDS["pressure"]["min_critical"] or press >= THRESHOLDS["pressure"]["max_critical"]:
            save_or_dedupe_alert("equipment", site_id, machine_id, "Critical Hydraulic Pressure Out of Range", "critical",
                                 {"metric": "pressure", "value": press, "unit": "bar"}, press_key)
        else:
            resolve_alert(press_key)


def mqtt_worker():
    """Connects to Mosquitto over TLS and listens for telemetry."""
    while not os.path.exists(MQTT_CA_FILE):
        log.info("Waiting for CA certificate at %s...", MQTT_CA_FILE)
        time.sleep(2)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="rules-engine")
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.tls_set(ca_certs=MQTT_CA_FILE, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.on_connect = on_mqtt_connect
    client.on_message = on_mqtt_message

    log.info("Connecting to MQTT broker %s:%d (TLS)...", MQTT_HOST, MQTT_PORT)
    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            client.loop_forever()
        except Exception as e:
            log.warning("MQTT connection error: %s. Reconnecting in 3s...", e)
            time.sleep(3)


# ----------------------------------------------------------------------
# 5. Watchdog Worker (Sensor Loss Detection)
# ----------------------------------------------------------------------
def watchdog_worker():
    """Detects when a machine stops sending telemetry (Threat: Service Unavailability)."""
    while True:
        time.sleep(5)
        now = time.time()
        with lock:
            machines = dict(last_seen_machines)

        for machine_id, data in machines.items():
            silence = now - data["last_seen"]
            if silence > SENSOR_TIMEOUT_S:
                site_id = data.get("site_id", "unknown")
                save_or_dedupe_alert(
                    source="equipment",
                    site_id=site_id,
                    machine_id=machine_id,
                    rule_name="Sensor Silence / Loss of Signal",
                    severity="critical",
                    details={"machine_id": machine_id, "silence_seconds": round(silence, 1), "timeout_limit": SENSOR_TIMEOUT_S},
                    dedupe_key=f"equipment:{site_id}:{machine_id}:sensor_loss"
                )


# ----------------------------------------------------------------------
# Main Application Entrypoint
# ----------------------------------------------------------------------
if __name__ == "__main__":
    # 1. Start HTTP healthcheck server for Docker (bind to 0.0.0.0)
    threading.Thread(
        target=ThreadingHTTPServer(("0.0.0.0", PORT), Health).serve_forever,
        daemon=True
    ).start()
    log.info("Health server listening on 0.0.0.0:%d", PORT)

    # 2. Initialize Database pool & schema
    init_db_pool()

    # 3. Start background workers
    threading.Thread(target=ids_worker, daemon=True, name="IDSWorker").start()
    threading.Thread(target=mqtt_worker, daemon=True, name="MQTTWorker").start()
    threading.Thread(target=watchdog_worker, daemon=True, name="WatchdogWorker").start()

    log.info("Rules engine fully operational. Listening for IDS events and MQTT telemetry...")

    while True:
        time.sleep(1)