"""Network checks against the isolated broker created by run.py (Paho 2.x)."""
import json
import socket
import ssl
import sys
import time

import paho.mqtt.client as mqtt

CA = "/certs/ca.crt"
PASSWORD = "isolated-test-password"
TELEMETRY = "factorysense/telemetry/test-site/test-machine"
STATUS = "factorysense/status/test-site/test-machine"
PAYLOAD = json.dumps({"ts": 1700000000000, "site_id": "test-site", "machine_id": "test-machine",
                      "temperature": -12.5, "pressure": 4.2, "vibration": 0.8}).encode()


def until(client, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "Timed out waiting for MQTT response"
        client.loop(timeout=0.1)


class Client:
    def __init__(self, user=None, password=PASSWORD, client_id="", persistent=False):
        self.messages = []
        self.connection = None
        self.session_present = False
        self.subscription = None
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                                  clean_session=not persistent, protocol=mqtt.MQTTv311)
        self.client.tls_set_context(ssl.create_default_context(cafile=CA))
        if user is not None:
            self.client.username_pw_set(user, password)
        self.client.on_connect = self.connected
        self.client.on_subscribe = self.subscribed
        self.client.on_message = lambda c, u, m: self.messages.append((m.topic, m.payload))
        self.client.connect("mosquitto", 8883, keepalive=10)
        until(self.client, lambda: self.connection is not None)

    def connected(self, client, userdata, flags, reason, properties):
        self.connection = reason
        self.session_present = flags.session_present

    def subscribed(self, client, userdata, mid, reasons, properties):
        self.subscription = reasons

    def subscribe(self, topic):
        self.subscription = None
        self.client.subscribe(topic, qos=1)
        until(self.client, lambda: self.subscription is not None)

    def publish(self, topic, payload):
        info = self.client.publish(topic, payload, qos=1, retain=False)
        until(self.client, info.is_published)

    def drain(self, seconds=0.5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.client.loop(timeout=0.1)

    def close(self):
        self.client.disconnect()
        self.client.loop(timeout=0.1)


def security_checks():
    for user, password in [(None, PASSWORD), ("gateway", "incorrect"), ("unknown", PASSWORD)]:
        denied = Client(user, password)
        assert denied.connection.is_failure, "Unauthenticated client connected"
        denied.close()
    print("PASS anonymous, wrong-password and unknown-user rejection", flush=True)

    for context, name in [(ssl.create_default_context(), "mosquitto"),
                          (ssl.create_default_context(cafile=CA), "wrong-hostname")]:
        try:
            with socket.create_connection(("mosquitto", 8883), timeout=3) as raw:
                with context.wrap_socket(raw, server_hostname=name):
                    raise AssertionError("Invalid TLS identity was accepted")
        except ssl.SSLCertVerificationError:
            pass
    for port in (1880, 1883):
        try:
            with socket.create_connection(("mosquitto", port), timeout=3):
                raise AssertionError(f"Unexpected network listener on {port}")
        except ConnectionRefusedError:
            pass
    # A plaintext MQTT CONNECT sent to the TLS port must never receive a CONNACK.
    with socket.create_connection(("mosquitto", 8883), timeout=3) as raw:
        raw.sendall(bytes.fromhex("100c00044d5154540402003c0000"))
        try:
            response = raw.recv(64)
            assert not response.startswith(b"\x20"), "Plaintext MQTT accepted on TLS port"
        except ConnectionResetError:
            pass
    print("PASS CA/hostname verification and TLS-only network listeners", flush=True)

    gateway, ingestor, rules = (Client(user) for user in ("gateway", "ingestor", "rules-engine"))
    for client in (gateway, ingestor, rules):
        assert not client.connection.is_failure
        # A broad subscription also checks that the broker filters disallowed deliveries.
        client.subscribe("factorysense/#")
    gateway.publish(TELEMETRY, PAYLOAD)
    gateway.publish(STATUS, b'{"rule":"overheating","state":"active"}')
    gateway.publish("factorysense/forbidden/test", b"forbidden")
    ingestor.publish(TELEMETRY, b"forbidden-ingestor-write")
    rules.publish(TELEMETRY, b"forbidden-rules-write")
    for client in (gateway, ingestor, rules):
        client.drain()
    assert gateway.messages == [], gateway.messages
    assert ingestor.messages == [(TELEMETRY, PAYLOAD)], ingestor.messages
    assert rules.messages == [(TELEMETRY, PAYLOAD),
                              (STATUS, b'{"rule":"overheating","state":"active"}')], rules.messages
    for client in (gateway, ingestor, rules):
        client.close()
    print("PASS all three roles: allowed routing, denied reads/writes, unchanged JSON", flush=True)


def prepare_restart():
    subscriber = Client("ingestor", client_id="persistence-test", persistent=True)
    subscriber.subscribe(TELEMETRY)
    subscriber.close()
    publisher = Client("gateway")
    publisher.publish(TELEMETRY, PAYLOAD)
    publisher.close()
    print("PASS queued QoS 1 message for an offline persistent subscriber", flush=True)


def verify_restart():
    subscriber = Client("ingestor", client_id="persistence-test", persistent=True)
    assert subscriber.session_present, "Broker lost the persistent session"
    until(subscriber.client, lambda: bool(subscriber.messages))
    assert subscriber.messages == [(TELEMETRY, PAYLOAD)], subscriber.messages
    subscriber.close()
    print("PASS session and original JSON/timestamp survived broker restart", flush=True)


{"security": security_checks, "prepare": prepare_restart, "verify": verify_restart}[sys.argv[1]]()
