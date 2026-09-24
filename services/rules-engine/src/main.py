"""Rules engine stub: detects anomalies (rules) and manages the resulting alerts, equipment and IDS alike."""
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE = os.getenv("SERVICE_NAME", "rules-engine")
PORT = int(os.getenv("PORT", "8000"))
CONFIG = {k: os.getenv(k) for k in ("MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_CA_FILE",
                                    "POSTGRES_HOST", "POSTGRES_DB", "POSTGRES_USER", "IDS_EVE_PATH")}

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
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", PORT), Health).serve_forever, daemon=True).start()
    log.info("started config=%s", CONFIG)  # passwords deliberately not logged
    while True:
        # TODO(phase 2) rules: subscribe to telemetry, compare against thresholds and an adaptive
        # baseline from a sliding window over the telemetry hypertable.
        # TODO(phase 2) alerts: merge those with new events tailed from IDS_EVE_PATH, dedupe, assign
        # severity, upsert into the alerts table with source = 'equipment' | 'ids', escalate if unacked.
        time.sleep(5)
