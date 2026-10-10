#!/usr/bin/env bash
# Local CA and broker certificate for the TLS listener on 8883 (R3, NFR-06).
#
#   tools/make_certs.sh                                    # into mosquitto/certs/
#   tools/make_certs.sh DNS:pdm-laptop.local IP:192.168.1.20   # names other machines use
#   PDM_CERTS_DIR=/some/dir tools/make_certs.sh            # somewhere else
#
# Writes ca.key and ca.crt (the local CA, kept on later runs) and server.key and server.crt
# (the broker's, renewed on every run), all P-256 ECDSA with SHA-256. server.crt names
# localhost, 127.0.0.1, ::1 and mosquitto (the compose service name). A client checks the
# name it connects to against this list, so a client on another machine needs the host's
# LAN name or address added as an argument. Keys are mode 0600, the folder 0700, and the
# folder stays out of git (.gitignore: mosquitto/certs/).
#
# Clients trust ca.crt: PDM_MQTT_TLS_CA=mosquitto/certs/ca.crt (pdm_common.broker), or
# mosquitto_sub --cafile mosquitto/certs/ca.crt -p 8883 ...
# Renewing: run it again (same CA, so clients keep working) and restart the broker.
# A new CA: delete ca.key and ca.crt first, then hand the new ca.crt to every client.
# Validity: PDM_CA_DAYS (default 1825) and PDM_SERVER_DAYS (default 365).
# Uses only openssl options that OpenSSL 1.1, 3.x and LibreSSL (macOS) share.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${PDM_CERTS_DIR:-$ROOT/mosquitto/certs}"
CA_DAYS="${PDM_CA_DAYS:-1825}"
SERVER_DAYS="${PDM_SERVER_DAYS:-365}"

command -v openssl >/dev/null || { echo "make_certs: openssl is not installed" >&2; exit 1; }
for san in "$@"; do
  case "$san" in
    DNS:?*|IP:?*) ;;
    *) echo "make_certs: '$san' should be DNS:<name> or IP:<address>" >&2; exit 2 ;;
  esac
done

mkdir -p "$OUT"
cd "$OUT"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

if [[ -f ca.key && -f ca.crt ]]; then
  echo "keeping the CA in $OUT (delete ca.key and ca.crt for a new one)"
else
  cat > "$work/ca.cnf" <<'EOF'
[req]
distinguished_name = dn
prompt = no
[dn]
CN = PdM-Agent local CA
[v3_ca]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
EOF
  openssl ecparam -name prime256v1 -genkey -noout -out ca.key
  openssl req -x509 -new -key ca.key -sha256 -days "$CA_DAYS" -config "$work/ca.cnf" -extensions v3_ca \
    -out ca.crt
  echo "new CA in $OUT/ca.crt"
fi

sans="DNS:localhost,DNS:mosquitto,IP:127.0.0.1,IP:::1"
for san in "$@"; do
  sans="$sans,$san"
done

cat > "$work/server.cnf" <<EOF
[req]
distinguished_name = dn
prompt = no
[dn]
CN = mosquitto
[v3_server]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = $sans
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid, issuer
EOF
openssl ecparam -name prime256v1 -genkey -noout -out server.key
openssl req -new -key server.key -config "$work/server.cnf" -out "$work/server.csr"
openssl x509 -req -in "$work/server.csr" -CA ca.crt -CAkey ca.key -set_serial "0x$(openssl rand -hex 16)" \
  -days "$SERVER_DAYS" -sha256 -extfile "$work/server.cnf" -extensions v3_server -out server.crt
chmod 600 ca.key server.key
chmod 644 ca.crt server.crt
openssl verify -CAfile ca.crt server.crt >/dev/null

echo "broker certificate $OUT/server.crt for $sans, valid until" \
  "$(openssl x509 -noout -enddate -in server.crt | cut -d= -f2)"
if git -C "$OUT" rev-parse --is-inside-work-tree >/dev/null 2>&1 && ! git -C "$OUT" check-ignore -q server.key; then
  echo "warning: git does not ignore $OUT; add it to .gitignore before committing anything" >&2
fi
