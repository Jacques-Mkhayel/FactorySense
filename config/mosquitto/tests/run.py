"""Run isolated broker integration checks. Requires Docker and the built gateway image.

Run from the repository root: python3 config/mosquitto/tests/run.py
Uses disposable credentials, a private network and dedicated volumes; never touches
project containers/data. The gateway image supplies Python and its pinned Paho dependency.
"""
from pathlib import Path
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[3]
PREFIX = "factorysense-broker-test-" + uuid.uuid4().hex[:10]
BROKER_IMAGE = "eclipse-mosquitto:2.1-alpine"
CLIENT_IMAGE = "factorysense-edge-gateway:latest"
PASSWORD = "isolated-test-password"
VOLUMES = [PREFIX + suffix for suffix in ("-server", "-ca", "-data")]
SERVER, CA, DATA = VOLUMES
BROKER = PREFIX + "-broker"


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)


def certificates(hostname="mosquitto", check=True):
    return docker("run", "--rm", "--network", "none", "-e", f"MQTT_TLS_HOSTNAME={hostname}",
                  "-v", f"{ROOT / 'config/mosquitto/gen-certs.sh'}:/gen-certs.sh:ro",
                  "-v", f"{SERVER}:/tls/server", "-v", f"{CA}:/tls/ca",
                  "python:3.12-slim", "sh", "/gen-certs.sh", check=check)


def ready():
    for _ in range(20):
        result = docker("exec", BROKER, "mosquitto_sub", "-h", "127.0.0.1", "-p", "1880",
                        "-t", "$SYS/broker/uptime", "-C", "1", "-W", "2", check=False)
        if result.returncode == 0:
            return
        time.sleep(0.2)
    raise AssertionError("Broker did not become healthy")


def clients(stage):
    result = docker("run", "--rm", "--network", PREFIX, "-v", f"{CA}:/certs:ro",
                    "-v", f"{ROOT / 'config/mosquitto/tests'}:/tests:ro",
                    "--entrypoint", "python", CLIENT_IMAGE, "/tests/client_checks.py", stage)
    print(result.stdout, end="", flush=True)


def main():
    try:
        docker("network", "create", "--internal", PREFIX)
        for volume in VOLUMES:
            docker("volume", "create", volume)
        certificates()
        reused = certificates()
        assert "Validated existing TLS material" in reused.stdout
        assert certificates("incorrect-hostname", check=False).returncode != 0
        print("PASS certificate creation, reuse and hostname mismatch rejection", flush=True)
        empty = docker("run", "--rm", "--network", "none",
                       "-v", f"{ROOT / 'config/mosquitto'}:/mosquitto/config:ro",
                       "--entrypoint", "sh", BROKER_IMAGE,
                       "/mosquitto/config/entrypoint.sh", check=False)
        assert empty.returncode != 0 and "must be non-empty" in empty.stderr
        print("PASS missing credential rejection", flush=True)
        env = []
        for name in ("MQTT_GATEWAY_PASSWORD", "MQTT_INGESTOR_PASSWORD", "MQTT_RULES_PASSWORD"):
            env.extend(["-e", f"{name}={PASSWORD}"])
        docker("run", "-d", "--name", BROKER, "--network", PREFIX,
               "--network-alias", "mosquitto", *env,
               "-v", f"{ROOT / 'config/mosquitto'}:/mosquitto/config:ro",
               "-v", f"{SERVER}:/mosquitto/tls:ro", "-v", f"{DATA}:/mosquitto/data",
               "--entrypoint", "sh", BROKER_IMAGE, "/mosquitto/config/entrypoint.sh",
               "/usr/sbin/mosquitto", "-c", "/mosquitto/config/mosquitto.conf")
        ready()
        mode = docker("exec", BROKER, "stat", "-c", "%a", "/tmp/mosquitto",
                      "/tmp/mosquitto/passwd", "/tmp/mosquitto/server.key")
        assert mode.stdout.split() == ["700", "600", "600"], mode.stdout
        print("PASS local healthcheck and private runtime file permissions", flush=True)
        clients("security")
        clients("prepare")
        docker("restart", "--time", "10", BROKER)
        ready()
        clients("verify")
        docker("run", "--rm", "--network", "none", "-v", f"{SERVER}:/tls",
               "python:3.12-slim", "sh", "-c",
               "openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:prime256v1 -out /tls/server.key")
        mismatch = certificates(check=False)
        assert mismatch.returncode != 0 and "does not match" in mismatch.stderr
        docker("run", "--rm", "--network", "none", "-v", f"{SERVER}:/tls",
               "python:3.12-slim", "rm", "/tls/server.key")
        partial = certificates(check=False)
        assert partial.returncode != 0 and "Incomplete TLS material" in partial.stderr
        print("PASS mismatched private key and incomplete certificate set rejection", flush=True)
        print("All isolated Mosquitto integration checks passed.", flush=True)
    except subprocess.CalledProcessError as error:
        print(error.stdout, error.stderr)
        print(docker("logs", BROKER, check=False).stdout)
        raise
    finally:
        docker("rm", "-f", BROKER, check=False)
        for volume in VOLUMES:
            docker("volume", "rm", volume, check=False)
        docker("network", "rm", PREFIX, check=False)


if __name__ == "__main__":
    main()
