"""Simulated PLC exposing periodically updated sensor input registers over Modbus TCP."""
import asyncio
import json
import logging
import math
import os
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext
from pymodbus.server import ModbusTcpServer

SERVICE = os.getenv("SERVICE_NAME", "simulator")
MACHINE_ID = os.getenv("MACHINE_ID", "press-01")
MODBUS_PORT = int(os.getenv("MODBUS_PORT", "502"))
UPDATE_INTERVAL_S = 1.0
SIMULATION_SCENARIO = os.getenv("SIMULATION_SCENARIO", "normal")

# Input-register addresses used by Modbus clients, including the edge gateway.
INPUT_REGISTER_MAP = {
    "temperature": 0,
    "pressure": 1,
    "vibration": 2,
}
INPUT_REGISTER_COUNT = 16  # Addresses 3 through 15 are reserved for future measurements.

# Each measurement occupies one 16-bit register, transferred as a raw value 0..65535.
# Temperature uses signed two's complement; pressure and vibration are unsigned.
# Vibration is a nonnegative vibration velocity magnitude, not a signed waveform.
MEASUREMENT_UNITS = {
    "temperature": "degC",
    "pressure": "bar",
    "vibration": "mm/s",
}
REGISTER_SCALES = {
    "temperature": 10,  # 71.5 degC -> 715; resolution 0.1 degC
    "pressure": 100,    # 4.25 bar -> 425; resolution 0.01 bar
    "vibration": 100,   # 0.83 mm/s -> 83; resolution 0.01 mm/s
}
REGISTER_SIGNED = {
    "temperature": True,
    "pressure": False,
    "vibration": False,
}
MAX_REGISTER_VALUE = 65535

# Illustrative normal-operation settings, expressed in the physical units above.
# These are demonstration values, not a model of a particular real machine.
SIMULATION_PROFILES = {
    "temperature": {"baseline": 70.0, "amplitude": 2.0, "period_s": 120.0, "noise": 0.2},
    "pressure": {"baseline": 4.2, "amplitude": 0.15, "period_s": 30.0, "noise": 0.02},
    "vibration": {"baseline": 0.8, "amplitude": 0.1, "period_s": 10.0, "noise": 0.02},
}


# Fault scenarios add a bounded, gradual offset to one measurement after an initial normal period.
# These are demonstration settings, not equipment limits or alert thresholds.
FAULT_START_S = 30.0
FAULT_SCENARIOS = {
    # name: (measurement, change per second, maximum total change)
    "overheating": ("temperature", 0.5, 30.0),     # levels off near 100 degC
    "vibration": ("vibration", 0.2, 7.0),          # worn bearing: levels off near 7.8 mm/s
    "pressure-drop": ("pressure", -0.05, -1.2),    # leak: levels off near 3.0 bar
    "pressure-spike": ("pressure", 0.05, 1.5),     # blocked line: levels off near 5.7 bar
}
SCENARIOS = ("normal", *FAULT_SCENARIOS)


def generate_measurements(
    elapsed_s: float,
    rng: random.Random | None = None,
    *,
    scenario: str = "normal",
) -> dict[str, float]:
    """Generate one set of physical readings for the elapsed simulation time.

    A sine wave provides a smooth cycle; bounded random noise adds small variations.
    Pass a seeded random.Random instance to reproduce a sequence for demonstrations
    or checks. A fault scenario adds a gradual, capped change to one measurement only.
    This function does not encode values, write registers or wait.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"SIMULATION_SCENARIO must be one of {', '.join(SCENARIOS)}")
    if not math.isfinite(elapsed_s) or elapsed_s < 0:
        raise ValueError("elapsed_s must be finite and nonnegative")
    noise_source = rng if rng is not None else random
    readings = {}
    for measurement, profile in SIMULATION_PROFILES.items():
        # Position within this measurement's cycle; sine varies between -1 and +1.
        phase = 2 * math.pi * ((elapsed_s % profile["period_s"]) / profile["period_s"])
        cycle = profile["amplitude"] * math.sin(phase)
        noise = noise_source.uniform(-profile["noise"], profile["noise"])
        readings[measurement] = profile["baseline"] + cycle + noise

    if scenario in FAULT_SCENARIOS:
        measurement, rate_per_s, max_change = FAULT_SCENARIOS[scenario]
        change = max(0.0, elapsed_s - FAULT_START_S) * rate_per_s
        # max_change carries the direction: cap a rise from above and a drop from below.
        readings[measurement] += min(change, max_change) if max_change > 0 else max(change, max_change)
    return readings


def encode_measurement(measurement: str, value: float) -> int:
    """Convert a physical value to a register integer, rounding to the nearest step."""
    scale = REGISTER_SCALES[measurement]
    signed = REGISTER_SIGNED[measurement]
    minimum = -32768 / scale if signed else 0
    maximum = 32767 / scale if signed else MAX_REGISTER_VALUE / scale
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(
            f"{measurement} must be finite and between {minimum} and {maximum} "
            f"{MEASUREMENT_UNITS[measurement]}"
        )
    scaled_value = round(value * scale)
    # Store negative signed values as their 16-bit two's-complement representation.
    return scaled_value + 65536 if scaled_value < 0 else scaled_value


def decode_measurement(measurement: str, raw_value: int) -> float:
    """Convert a register integer back to its physical unit (the gateway's contract)."""
    scale = REGISTER_SCALES[measurement]
    if type(raw_value) is not int or not 0 <= raw_value <= MAX_REGISTER_VALUE:
        raise ValueError(f"Register value must be an integer between 0 and {MAX_REGISTER_VALUE}")
    # Raw values with the top bit set represent negatives only for signed measurements.
    if REGISTER_SIGNED[measurement] and raw_value >= 32768:
        raw_value -= 65536
    return raw_value / scale


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


async def update_registers(server: ModbusTcpServer, elapsed_s: float) -> None:
    """Generate and encode one sample, then replace the three sensor registers together."""
    readings = generate_measurements(elapsed_s, scenario=SIMULATION_SCENARIO)
    values = [0] * len(INPUT_REGISTER_MAP)
    for measurement, address in INPUT_REGISTER_MAP.items():
        values[address] = encode_measurement(measurement, readings[measurement])

    # Device 1 is the gateway's configured Modbus device ID. Function code 4 selects
    # input registers; this is an internal update, not a Modbus write from a client.
    # Only addresses 0..2 are replaced; reserved registers 3..15 are left alone.
    await server.async_setValues(device_id=1, func_code=4, address=0, values=values)
    log.debug("updated elapsed_s=%.2f readings=%s registers=%s", elapsed_s, readings, values)


async def run_simulator() -> None:
    """Serve Modbus requests while refreshing measurements approximately once a second."""
    # Sensor values live in input registers, which Modbus clients cannot write. Like most real PLCs, the
    # holding registers pymodbus adds by default still accept unauthenticated writes: "read-only" is the
    # gateway's policy, and Suricata alerts on any write (sid 1000002).
    # pymodbus data blocks are 1-based: block address 1 is Modbus register 0.
    registers = ModbusDeviceContext(ir=ModbusSequentialDataBlock(1, [0] * INPUT_REGISTER_COUNT))
    server = ModbusTcpServer(ModbusServerContext(devices=registers), address=("0.0.0.0", MODBUS_PORT))
    loop = asyncio.get_running_loop()
    started_at = loop.time()  # Monotonic time: unaffected by wall-clock corrections.

    try:
        # Make the first sample available before accepting client connections.
        await update_registers(server, elapsed_s=0.0)
        await server.serve_forever(background=True)
        log.info("sampling update_interval_s=%s", UPDATE_INTERVAL_S)
        while True:
            # Yield to the server so it can respond to clients during this wait.
            await asyncio.sleep(UPDATE_INTERVAL_S)
            await update_registers(server, elapsed_s=loop.time() - started_at)
    finally:
        await server.shutdown()


if __name__ == "__main__":
    # Health endpoint on loopback only: the PLC exposes nothing but Modbus to the network.
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", 8000), Health).serve_forever, daemon=True).start()
    log.info("starting machine_id=%s modbus_port=%d", MACHINE_ID, MODBUS_PORT)
    log.info("input_register_map=%s", INPUT_REGISTER_MAP)
    log.info("simulation_scenario=%s", SIMULATION_SCENARIO)
    if SIMULATION_SCENARIO in FAULT_SCENARIOS:
        measurement, rate_per_s, max_change = FAULT_SCENARIOS[SIMULATION_SCENARIO]
        log.info("fault measurement=%s start_s=%s rate_per_s=%s max_change=%s",
                 measurement, FAULT_START_S, rate_per_s, max_change)
    asyncio.run(run_simulator())
