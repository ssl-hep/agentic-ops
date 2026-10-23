#!/usr/bin/env bash
# Print the phase 0 manifests with the image and public host filled in.
#   phase0/deploy/render.sh IMAGE HOST [ingress|lb] | kubectl apply -f -
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
image=${1:?image, e.g. ghcr.io/ssl-hep/agentic-ops-temporal-phase0:<tag>}
host=${2:?public host name, e.g. temporal-phase0.example.org}
expose=${3:-ingress}
case $expose in
  ingress) files=("$here/server.yaml" "$here/ingress.yaml") ;;
  lb) files=("$here/server.yaml" "$here/service-lb.yaml") ;;
  *) echo "third argument must be ingress or lb" >&2; exit 2 ;;
esac
for f in "${files[@]}"; do
  echo ---
  sed -e "s|__IMAGE__|$image|g" -e "s|__HOST__|$host|g" "$f"
done
