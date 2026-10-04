#!/bin/sh
# Builds the password file from environment variables at start-up,
# so no credential, not even a hash, is ever committed to the repository.
# Also copies the ACL and the TLS key out of their read-only mounts: mosquitto wants
# these files owned by it and not world-readable.
set -eu

# Fail before creating any files if a required credential is absent or empty.
: "${MQTT_GATEWAY_PASSWORD:?MQTT_GATEWAY_PASSWORD must be non-empty}"
: "${MQTT_INGESTOR_PASSWORD:?MQTT_INGESTOR_PASSWORD must be non-empty}"
: "${MQTT_RULES_PASSWORD:?MQTT_RULES_PASSWORD must be non-empty}"
# Copies of private keys and password hashes must be private from creation.
umask 077

RUNTIME_DIR=/tmp/mosquitto
PASSWD_FILE="$RUNTIME_DIR/passwd"
mkdir -p "$RUNTIME_DIR"
chmod 0700 "$RUNTIME_DIR"
cp /mosquitto/config/acl "$RUNTIME_DIR/acl"
cp /mosquitto/tls/server.key "$RUNTIME_DIR/server.key"
: > "$PASSWD_FILE"
chmod 0600 "$PASSWD_FILE"

mosquitto_passwd -b "$PASSWD_FILE" gateway "$MQTT_GATEWAY_PASSWORD"
mosquitto_passwd -b "$PASSWD_FILE" ingestor "$MQTT_INGESTOR_PASSWORD"
mosquitto_passwd -b "$PASSWD_FILE" rules-engine "$MQTT_RULES_PASSWORD"
chmod 0600 "$RUNTIME_DIR/acl" "$RUNTIME_DIR/server.key"
chown -R mosquitto:mosquitto "$RUNTIME_DIR"

# Hand over to the image's own entrypoint (fixes data dir ownership, then runs mosquitto).
exec /docker-entrypoint.sh "$@"
