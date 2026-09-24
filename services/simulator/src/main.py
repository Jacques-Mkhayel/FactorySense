"""Simulator stub: stands in for a PLC exposing a read-only register map over Modbus TCP."""
import asyncio
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext
from pymodbus.server import StartAsyncTcpServer

SERVICE = os.getenv("SERVICE_NAME", "simulator")
MACHINE_ID = os.getenv("MACHINE_ID", "press-01")
MODBUS_PORT = int(os.getenv("MODBUS_PORT", "502"))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s level=%(levelname)s service=%(name)s msg=%(message)s")
log = logging.getLogger(SERVICE)


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = (200, {"status": "ok"}) if self.path == "/health" else (404, {"error": "not found"})
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)


if __name__ == "__main__":
    # Health endpoint on loopback only: the PLC exposes nothing but Modbus to the network.
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", 8000), Health).serve_forever, daemon=True).start()
    # Sensor values live in input registers, which Modbus clients cannot write. Like most real PLCs, the
    # holding registers pymodbus adds by default still accept unauthenticated writes: "read-only" is the
    # gateway's policy, and Suricata alerts on any write (sid 1000002).
    # pymodbus data blocks are 1-based: block address 1 is Modbus register 0.
    # TODO(phase 2): update vibration, temperature and pressure registers on a timer.
    registers = ModbusDeviceContext(ir=ModbusSequentialDataBlock(1, [0] * 16))
    log.info("starting machine_id=%s modbus_port=%d", MACHINE_ID, MODBUS_PORT)
    asyncio.run(StartAsyncTcpServer(ModbusServerContext(devices=registers), address=("0.0.0.0", MODBUS_PORT)))
