#!/usr/bin/env bash
# Make the phase 0 CA, the frontend's server certificate and the client certificates.
#
#   phase0/certs/mkcerts.sh [-o OUT_DIR] [-n SERVER_NAME]... [-i SERVER_IP]... [-d WORKER_DAYS]
#
# OUT_DIR defaults to secrets/phase0-certs (gitignored). Every -n adds a DNS name and every -i
# an IP address to the server certificate (e.g. the LoadBalancer address, for clients that
# verify against the IP); localhost, host.docker.internal and 127.0.0.1 are always included.
# Re-running keeps an existing CA and replaces everything else.
#
# Subjects follow temporal-server/authz: O=agentic-ops, OU=<role>, CN=<name>.
#   clients/admin          OU=admin       registers namespaces; never leaves the operator's machine
#   clients/controller     OU=controller  starts and runs workflows
#   clients/ewkit-phase0   OU=worker      polls ewkit-phase0-<memclass>
#   clients/ewkit-phase1   OU=worker      a second production, for the negative tests
#   clients/af-io          OU=worker      polls af-io
#   clients/foreign        signed by a different CA, must be rejected at the handshake
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/../.." && pwd)
out="$repo_root/secrets/phase0-certs"
names=()
ips=()
worker_days=7

while getopts "o:n:i:d:" opt; do
  case $opt in
    o) out=$OPTARG ;;
    n) [[ $OPTARG =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || { echo "not a DNS name: $OPTARG" >&2; exit 2; }
       names+=("$OPTARG") ;;
    i) [[ $OPTARG =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "not an IPv4 address: $OPTARG" >&2; exit 2; }
       ips+=("$OPTARG") ;;
    d) worker_days=$OPTARG ;;
    *) sed -n '2,9p' "$0"; exit 2 ;;
  esac
done

umask 077
mkdir -p "$out/server" "$out/clients"
cd "$out"

key() { openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$1" 2>/dev/null; }

if [[ ! -f ca.pem ]]; then
  key ca-key.pem
  openssl req -x509 -new -key ca-key.pem -sha256 -days 365 -out ca.pem \
    -subj "/O=agentic-ops/OU=phase0/CN=agentic-ops phase0 CA" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign"
fi

sign() { # sign <csr> <cert> <days> <extfile> [ca] [ca-key]
  openssl x509 -req -in "$1" -CA "${5:-ca.pem}" -CAkey "${6:-ca-key.pem}" -CAcreateserial \
    -sha256 -days "$3" -extfile "$4" -out "$2" 2>/dev/null
}

# Server
san="DNS:localhost,DNS:host.docker.internal,IP:127.0.0.1"
for n in "${names[@]+"${names[@]}"}"; do san+=",DNS:$n"; done
for i in "${ips[@]+"${ips[@]}"}"; do san+=",IP:$i"; done
key server/server-key.pem
openssl req -new -key server/server-key.pem -subj "/O=agentic-ops/OU=server/CN=temporal-phase0" -out server/server.csr
printf 'basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=%s\n' "$san" > server/ext.cnf
sign server/server.csr server/server.pem 90 server/ext.cnf
cp ca.pem server/ca.pem
rm server/server.csr server/ext.cnf

client() { # client <dir> <role> <cn> <days> [ca] [ca-key]
  local d="clients/$1"
  mkdir -p "$d"
  key "$d/key.pem"
  openssl req -new -key "$d/key.pem" -subj "/O=agentic-ops/OU=$2/CN=$3" -out "$d/req.csr"
  printf 'basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\n' > "$d/ext.cnf"
  sign "$d/req.csr" "$d/cert.pem" "$4" "$d/ext.cnf" "${5:-}" "${6:-}"
  cp ca.pem "$d/ca.pem"
  rm "$d/req.csr" "$d/ext.cnf"
}

client admin admin admin 30
client controller controller controller 30
client ewkit-phase0 worker ewkit-phase0 "$worker_days"
client ewkit-phase1 worker ewkit-phase1 "$worker_days"
client af-io worker af-io "$worker_days"

# A client from an unrelated CA with an otherwise valid admin subject
mkdir -p foreign-ca
[[ -f foreign-ca/ca.pem ]] || {
  key foreign-ca/ca-key.pem
  openssl req -x509 -new -key foreign-ca/ca-key.pem -sha256 -days 365 -out foreign-ca/ca.pem \
    -subj "/O=agentic-ops/CN=not our CA" -addext "basicConstraints=critical,CA:TRUE"
}
client foreign admin admin 1 foreign-ca/ca.pem foreign-ca/ca-key.pem
cp ca.pem clients/foreign/ca.pem

# The server reads its key as a non-root user from a mounted directory
chmod 644 server/*.pem
echo "Certificates in $out (server SANs: $san)"
