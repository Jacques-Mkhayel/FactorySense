#!/bin/sh
# One-shot job: creates a throwaway CA and the broker's TLS certificate on first start.
# The CA private key is never persisted, so nothing can mint further certificates.
# To rotate: `docker compose down`, remove the mqtt-tls-* volumes, start again.
set -eu
umask 077
: "${MQTT_TLS_HOSTNAME:?MQTT_TLS_HOSTNAME must be non-empty}"
# This prototype issues a single DNS-name certificate, not IP or wildcard certificates.
case "$MQTT_TLS_HOSTNAME" in
    *[!a-zA-Z0-9.-]*|.*|-*) echo "Invalid MQTT_TLS_HOSTNAME" >&2; exit 1 ;;
esac

SERVER_DIR=/tls/server
CA_DIR=/tls/ca

# Never silently replace only half of an existing certificate set: clients may still
# trust the old CA. Fail clearly so the operator can repair/rotate the set together.
if [ -e "$SERVER_DIR/server.crt" ] || [ -e "$SERVER_DIR/server.key" ] || [ -e "$CA_DIR/ca.crt" ]; then
    for file in "$SERVER_DIR/server.crt" "$SERVER_DIR/server.key" "$CA_DIR/ca.crt"; do
        if [ ! -s "$file" ]; then
            echo "Incomplete TLS material: missing or empty $file. Restore or rotate the complete TLS set." >&2
            exit 1
        fi
    done
    openssl verify -CAfile "$CA_DIR/ca.crt" -purpose sslserver \
        -verify_hostname "$MQTT_TLS_HOSTNAME" "$SERVER_DIR/server.crt"
    CERT_PUBLIC=$(openssl x509 -in "$SERVER_DIR/server.crt" -pubkey -noout)
    KEY_PUBLIC=$(openssl pkey -in "$SERVER_DIR/server.key" -pubout)
    if [ "$CERT_PUBLIC" != "$KEY_PUBLIC" ]; then
        echo "TLS private key does not match the server certificate." >&2
        exit 1
    fi
    echo "Validated existing TLS material for ${MQTT_TLS_HOSTNAME}"
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
