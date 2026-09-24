#!/bin/sh
# One-shot job: creates a throwaway CA and the broker's TLS certificate on first start.
# The CA private key is never persisted, so nothing can mint further certificates.
# To rotate: `docker compose down`, remove the mqtt-tls-* volumes, start again.
set -eu

SERVER_DIR=/tls/server
CA_DIR=/tls/ca

if [ -s "$SERVER_DIR/server.crt" ] && [ -s "$CA_DIR/ca.crt" ]; then
    echo "TLS material already present for ${MQTT_TLS_HOSTNAME}, nothing to do"
    exit 0
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 365 \
    -subj "/CN=FactorySense prototype CA" -keyout "$WORK/ca.key" -out "$WORK/ca.crt" 2>/dev/null
openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -subj "/CN=${MQTT_TLS_HOSTNAME}" -keyout "$WORK/server.key" -out "$WORK/server.csr" 2>/dev/null
# Clients verify the broker by the name they dial, so it must be in the SAN.
printf 'subjectAltName=DNS:%s\nextendedKeyUsage=serverAuth\n' "$MQTT_TLS_HOSTNAME" > "$WORK/ext.cnf"
openssl x509 -req -in "$WORK/server.csr" -CA "$WORK/ca.crt" -CAkey "$WORK/ca.key" -CAcreateserial \
    -days 365 -extfile "$WORK/ext.cnf" -out "$WORK/server.crt" 2>/dev/null

install -m 0644 "$WORK/ca.crt" "$CA_DIR/ca.crt"
install -m 0644 "$WORK/server.crt" "$SERVER_DIR/server.crt"
install -m 0600 "$WORK/server.key" "$SERVER_DIR/server.key"
echo "Generated prototype CA and server certificate for ${MQTT_TLS_HOSTNAME}"
