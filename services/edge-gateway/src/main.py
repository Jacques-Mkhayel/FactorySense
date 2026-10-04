"""Collect PLC readings, check local conditions, and durably replay MQTT/TLS messages."""
import json
import logging
import math
import os
import signal
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from local_checks import LocalProcessor
from outbox import Outbox
from publisher import ReplayWorker

SERVICE = os.getenv("SERVICE_NAME", "edge-gateway")
PORT = int(os.getenv("PORT", "8000"))
CONFIG = {k: os.getenv(k) for k in ("SITE_ID", "MACHINE_ID", "MODBUS_HOST", "MODBUS_PORT", "MODBUS_UNIT_ID", "POLL_INTERVAL_S",
                                    "MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_CA_FILE",
                                    "BUFFER_PATH", "BUFFER_RETENTION_H", "MQTT_ACK_TIMEOUT_S",
                                    "LOCAL_WINDOW_S", "LOCAL_TEMP_CRITICAL_C", "LOCAL_TEMP_CLEAR_C")}

# Agreed simulator layout: input registers 0=temperature, 1=pressure, 2=vibration.
SENSOR_REGISTER_START = 0
SENSOR_REGISTER_COUNT = 3
MODBUS_TIMEOUT_S = 3.0
MQTT_MAX_QUEUED_MESSAGES = 1  # SQLite owns the backlog; only one message is in flight.
STATUS_LOCK = threading.Lock()
STATUS = {
    "mqtt_connected": False, "modbus_connected": False, "buffered_messages": 0,
    "last_sample_ts": None, "local_alerts": {}, "worker_error": None,
}


def update_status(**fields):
    with STATUS_LOCK:
        STATUS.update(fields)


def health_snapshot():
    with STATUS_LOCK:
        snapshot = dict(STATUS)
    snapshot["status"] = "ok" if snapshot["mqtt_connected"] and snapshot["modbus_connected"] else "degraded"
    if snapshot["worker_error"]:
        snapshot["status"] = "error"
    return snapshot


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s level=%(levelname)s service=%(name)s msg=%(message)s")
log = logging.getLogger(SERVICE)


def build_telemetry_message(
    *,
    site_id: str,
    machine_id: str,
    measured_at_ms: int,
    temperature: float,
    pressure: float,
    vibration: float,
) -> str:
    """Build JSON from decoded readings: degC, bar and mm/s, with acquisition time.

    Capture measured_at_ms once immediately after a successful read, using
    time.time_ns() // 1_000_000. This is gateway collection time, not a PLC timestamp.
    Preserve that timestamp if delivery is delayed.
    This function only formats the message; it does not read Modbus or publish MQTT.
    """
    for name, value in (("site_id", site_id), ("machine_id", machine_id)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a nonempty string")
    if type(measured_at_ms) is not int or measured_at_ms < 0:
        raise ValueError("measured_at_ms must be a nonnegative integer (Unix milliseconds)")

    readings = {
        "temperature": temperature,
        "pressure": pressure,
        "vibration": vibration,
    }
    for name, value in readings.items():
        if type(value) not in (int, float):
            raise ValueError(f"{name} must be a number in its physical unit")

    payload = {
        "ts": measured_at_ms,
        "site_id": site_id,
        "machine_id": machine_id,
        **readings,
    }
    # NaN and infinity are not valid JSON measurements.
    return json.dumps(payload, allow_nan=False)


def read_sensor_registers(client: ModbusTcpClient, unit_id: int) -> list[int]:
    """Read one complete raw sample; raise an error instead of returning invalid data."""
    response = client.read_input_registers(
        SENSOR_REGISTER_START,
        count=SENSOR_REGISTER_COUNT,
        device_id=unit_id,
    )
    if response.isError():
        raise ModbusException(f"Input-register read failed: {response}")
    if len(response.registers) != SENSOR_REGISTER_COUNT:
        raise ModbusException(
            f"Expected {SENSOR_REGISTER_COUNT} registers, received {len(response.registers)}"
        )
    return response.registers


def decode_sensor_registers(raw_registers: list[int]) -> dict[str, float]:
    """Decode the simulator's register contract into degC, bar and mm/s.

    Register 0 is signed 16-bit temperature scaled by 10. Registers 1 and 2
    are unsigned pressure and vibration values scaled by 100.
    """
    if len(raw_registers) != SENSOR_REGISTER_COUNT:
        raise ValueError(f"Expected {SENSOR_REGISTER_COUNT} sensor registers")
    if any(type(value) is not int or not 0 <= value <= 65535 for value in raw_registers):
        raise ValueError("Raw registers must be integers between 0 and 65535")

    temperature_raw, pressure_raw, vibration_raw = raw_registers
    # Decode two's complement only for temperature; pressure/vibration stay unsigned.
    if temperature_raw >= 32768:
        temperature_raw -= 65536
    return {
        "temperature": temperature_raw / 10,
        "pressure": pressure_raw / 100,
        "vibration": vibration_raw / 100,
    }


def build_telemetry_topic(site_id: str, machine_id: str) -> str:
    """Keep each source identifier in one literal MQTT topic segment."""
    for name, value in (("SITE_ID", site_id), ("MACHINE_ID", machine_id)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be configured as a nonempty string")
        if any(character in value for character in ("/", "+", "#", "\x00")):
            raise ValueError(f"{name} must not contain /, +, # or a null character")
    topic = f"factorysense/telemetry/{site_id}/{machine_id}"
    if len(topic.encode("utf-8")) > 65535:
        raise ValueError("Telemetry topic exceeds the MQTT length limit")
    return topic


def on_mqtt_publish(client, userdata, mid, reason_code, properties):
    """For our QoS 1 publishes, Paho calls this after the broker's PUBACK handshake."""
    if reason_code.is_failure:
        log.warning("mqtt_publish_rejected mid=%d reason=%s", mid, reason_code)
    else:
        log.info("mqtt_publish_acknowledged mid=%d", mid)
        if userdata is not None:
            userdata.put(mid)  # Replay worker, not the network callback, commits SQLite deletes.


def on_mqtt_connect(client, userdata, flags, reason_code, properties):
    """Called after the broker replies to the MQTT connection request."""
    update_status(mqtt_connected=not reason_code.is_failure)
    if reason_code.is_failure:
        log.warning("mqtt_connection_rejected reason=%s", reason_code)
    else:
        log.info("mqtt_connected host=%s port=%s tls=true", client.host, client.port)


def on_mqtt_connect_fail(client, userdata):
    update_status(mqtt_connected=False)
    log.warning("mqtt_connection_failed host=%s port=%s; check network/TLS; retrying",
                client.host, client.port)


def on_mqtt_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    update_status(mqtt_connected=False)
    if reason_code.is_failure:
        log.warning("mqtt_disconnected reason=%s; retrying", reason_code)
    else:
        log.info("mqtt_disconnected reason=%s", reason_code)


def create_mqtt_client(site_id: str, machine_id: str, acknowledgements=None) -> mqtt.Client:
    """Configure broker verification and gateway authentication before any connection."""
    username = CONFIG["MQTT_USER"]
    ca_file = CONFIG["MQTT_CA_FILE"]
    # Keep the password outside CONFIG: CONFIG is printed at startup.
    password = os.getenv("MQTT_PASSWORD")
    for name, value in (("MQTT_USER", username), ("MQTT_CA_FILE", ca_file)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be configured as a nonempty string")
    if not password:
        raise ValueError("MQTT_PASSWORD must be configured")

    # Trust the configured CA and require a certificate matching MQTT_HOST.
    # No client certificate is needed: Mosquitto authenticates us by username/password.
    tls_context = ssl.create_default_context(cafile=ca_file)
    tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"factorysense-gateway-{site_id}-{machine_id}",
        protocol=mqtt.MQTTv311,
        clean_session=True,
        userdata=acknowledgements,
    )
    client.tls_set_context(tls_context)
    client.username_pw_set(username, password)
    client.on_connect = on_mqtt_connect
    client.on_connect_fail = on_mqtt_connect_fail
    client.on_disconnect = on_mqtt_disconnect
    client.on_publish = on_mqtt_publish
    client.max_queued_messages_set(MQTT_MAX_QUEUED_MESSAGES)
    client.connect_timeout = 3.0
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    return client


def start_mqtt_client(site_id: str, machine_id: str, acknowledgements=None) -> mqtt.Client:
    """Start connection attempts in a background thread so Modbus polling can continue."""
    host = CONFIG["MQTT_HOST"]
    port = int(CONFIG["MQTT_PORT"] or 8883)
    if not isinstance(host, str) or not host.strip():
        raise ValueError("MQTT_HOST must be configured as a nonempty string")
    if not 1 <= port <= 65535:
        raise ValueError("MQTT_PORT must be between 1 and 65535")
    client = create_mqtt_client(site_id, machine_id, acknowledgements)
    client.connect_async(host, port=port, keepalive=60)
    log.info("mqtt_connecting host=%s port=%d tls=true", host, port)
    if client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError("Could not start the MQTT network loop")
    return client


def numeric_setting(name, default, positive=True):
    value = float(CONFIG.get(name) or default)
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"{name} must be finite" + (" and greater than zero" if positive else ""))
    return value


def run_gateway(stop=None) -> None:
    """Collect and save locally while an independent worker drains the durable outbox."""
    stop = stop if stop is not None else threading.Event()
    site_id, machine_id = CONFIG["SITE_ID"], CONFIG["MACHINE_ID"]
    topic = build_telemetry_topic(site_id, machine_id)
    status_topic = f"factorysense/status/{site_id}/{machine_id}"
    host = CONFIG["MODBUS_HOST"] or "simulator"
    port = int(CONFIG["MODBUS_PORT"] or 502)
    unit_id = int(CONFIG["MODBUS_UNIT_ID"] or 1)
    if not 1 <= port <= 65535 or not 0 <= unit_id <= 255:
        raise ValueError("Invalid MODBUS_PORT or MODBUS_UNIT_ID")
    poll_interval_s = numeric_setting("POLL_INTERVAL_S", 1)
    retention_h = numeric_setting("BUFFER_RETENTION_H", 72)
    ack_timeout_s = numeric_setting("MQTT_ACK_TIMEOUT_S", 30)
    window_s = numeric_setting("LOCAL_WINDOW_S", 60)
    critical_c = numeric_setting("LOCAL_TEMP_CRITICAL_C", 85, positive=False)
    clear_c = numeric_setting("LOCAL_TEMP_CLEAR_C", 80, positive=False)
    if clear_c >= critical_c:
        raise ValueError("LOCAL_TEMP_CLEAR_C must be less than LOCAL_TEMP_CRITICAL_C")
    path = CONFIG["BUFFER_PATH"] or "/var/lib/gateway/buffer.db"
    # Configuration/TLS errors are fatal; never downgrade security to keep running.
    create_mqtt_client(site_id, machine_id)
    outbox = Outbox(path)
    client = ModbusTcpClient(host, port=port, timeout=MODBUS_TIMEOUT_S, retries=0)
    worker = ReplayWorker(outbox, lambda acks: start_mqtt_client(site_id, machine_id, acks),
                          retention_h, ack_timeout_s, stop, update_status)
    try:
        processor = LocalProcessor(window_s, critical_c, clear_c, outbox.state(topic))
        update_status(local_alerts=dict(processor.state), buffered_messages=outbox.count(), worker_error=None)
        worker.start()
        worker.ready.wait()
        if worker.failure:
            raise RuntimeError("MQTT replay worker could not start") from worker.failure
        failures = 0
        while not stop.is_set():
            try:
                if not client.connect():
                    raise ConnectionError(f"Cannot connect to {host}:{port}")
                raw = read_sensor_registers(client, unit_id)
                measured_at_ms = time.time_ns() // 1_000_000
                measurements = decode_sensor_registers(raw)
            except (OSError, ModbusException, ValueError) as exc:
                failures += 1
                delay = min(30.0, max(1.0, poll_interval_s) * 2 ** min(failures - 1, 5))
                update_status(modbus_connected=False)
                log.warning("modbus_read_failed error=%s retry_in_s=%s", exc, delay)
                client.close()
                stop.wait(delay)
                continue
            if failures:
                log.info("modbus_recovered failed_attempts=%d", failures)
            failures = 0
            features, transition = processor.evaluate(measurements, time.monotonic())
            message = build_telemetry_message(site_id=site_id, machine_id=machine_id,
                                              measured_at_ms=measured_at_ms, **measurements)
            messages = [(topic, message)]
            if transition:
                event = {
                    "ts": measured_at_ms, "site_id": site_id, "machine_id": machine_id,
                    "kind": "local_alert", "rule": "overheating", "state": transition,
                    "severity": "critical" if transition == "active" else "info",
                    "temperature": measurements["temperature"],
                    "critical_c": critical_c, "clear_c": clear_c, "features": features,
                }
                messages.append((status_topic, json.dumps(event, allow_nan=False)))
                log.warning("local_alert=%s", messages[-1][1])
            # SQLite failures are fatal, not silently treated as a lost network sample.
            # Alarm state and its transition event commit in the same transaction.
            outbox.record(measured_at_ms, messages, topic, processor.state)
            update_status(modbus_connected=True, last_sample_ts=measured_at_ms,
                          local_alerts=dict(processor.state), buffered_messages=outbox.count())
            log.info("telemetry_buffered=%s", message)
            log.info("local_features=%s", json.dumps(features))
            stop.wait(poll_interval_s)
        if worker.failure:
            raise RuntimeError("MQTT replay worker failed") from worker.failure
    finally:
        stop.set()
        client.close()
        if worker.ident is not None:
            worker.join()  # Network attempts have bounded timeouts; preserve unacknowledged rows.
        outbox.close()


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = (200, health_snapshot()) if self.path == "/health" else (404, {"error": "not found"})
        if body.get("status") == "error":
            status = 503
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)


if __name__ == "__main__":
    # Loopback only: the gateway accepts no inbound connection from either network (outbound only).
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", PORT), Health).serve_forever, daemon=True).start()
    log.info("started config=%s", CONFIG)  # MQTT_PASSWORD deliberately not logged
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run_gateway(stop)
