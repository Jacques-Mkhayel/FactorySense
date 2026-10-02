# Starlark runs inside Telegraf. Keep the original metric so MQTT delivery tracking
# follows it through validation and the PostgreSQL output.
load("json.star", "json")
load("logging.star", "log")

SENSORS = {
    "temperature": (-3276.8, 3276.7),  # signed 16-bit register / 10, degrees C
    "pressure": (0.0, 655.35),        # unsigned 16-bit register / 100, bar
    "vibration": (0.0, 655.35),       # unsigned 16-bit register / 100, mm/s
}

def reject(reason):
    # Do not log the raw payload or credentials. Invalid messages are deliberately
    # dropped/acknowledged, so a malformed message cannot block the MQTT queue.
    log.warn("Rejected telemetry: " + reason)
    return None

def apply(metric):
    message = json.decode(metric.fields.get("value", ""), default=None)
    if type(message) != "dict":
        return reject("expected a JSON object")

    ts = message.get("ts")
    # Telegraf stores nanoseconds in a signed 64-bit timestamp; never substitute
    # arrival time when the gateway's measurement timestamp is absent/invalid.
    if type(ts) != "int" or ts <= 0 or ts > 9223372036854:
        return reject("ts must be a positive Unix-millisecond integer in range")

    for key in ("site_id", "machine_id"):
        value = message.get(key)
        if type(value) != "string" or not value or any([c in value for c in ("/", "+", "#", "\x00")]):
            return reject("invalid " + key)
    topic = "factorysense/telemetry/" + message["site_id"] + "/" + message["machine_id"]
    if metric.tags.get("mqtt_topic") != topic:
        return reject("topic and payload identifiers do not match")

    for name, limits in SENSORS.items():
        value = message.get(name)
        if type(value) not in ("int", "float"):
            return reject(name + " must be a JSON number")
        # The comparison also rejects non-finite values. These are encoding limits,
        # not alarm thresholds: overheating readings must still reach the database.
        if not (limits[0] <= value and value <= limits[1]):
            return reject(name + " exceeds the agreed register encoding range")

    metric.name = "telemetry"
    metric.time = ts * 1000000
    metric.tags.clear()
    metric.tags["site_id"] = message["site_id"]
    metric.tags["machine_id"] = message["machine_id"]
    metric.fields.clear()
    for name in SENSORS:
        metric.fields[name] = float(message[name])
    return metric
