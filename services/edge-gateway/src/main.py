"""Edge gateway stub: polls the PLC over Modbus, buffers on WAN loss, publishes to the cloud broker."""
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE = os.getenv("SERVICE_NAME", "edge-gateway")
PORT = int(os.getenv("PORT", "8000"))
CONFIG = {k: os.getenv(k) for k in ("SITE_ID", "MODBUS_HOST", "MODBUS_PORT", "MODBUS_UNIT_ID", "POLL_INTERVAL_S",
                                    "MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_CA_FILE",
                                    "BUFFER_PATH", "BUFFER_RETENTION_H")}

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s level=%(levelname)s service=%(name)s msg=%(message)s")
log = logging.getLogger(SERVICE)


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = (200, {"status": "ok"}) if self.path == "/health" else (404, {"error": "not found"})
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
    while True:
        # TODO(phase 2): read input registers over Modbus, compute features, evaluate local rules
        # (critical thresholds still fire during a WAN outage), publish over MQTT/TLS;
        # on broker loss append to BUFFER_PATH (kept BUFFER_RETENTION_H hours) and backfill with original timestamps.
        time.sleep(float(CONFIG["POLL_INTERVAL_S"] or 1))
