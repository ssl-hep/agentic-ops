#!/usr/bin/env bash
# Build the self-contained worker environment that HTCondor jobs unpack on the AF:
# a relocatable CPython (python-build-standalone, installed by uv), the locked packages in
# requirements.txt, and the phase 0 shim. Nothing on the worker node is used but glibc.
#
#   phase0/workerenv/build.sh            # in an almalinux:9 linux/amd64 container (needs docker)
#   phase0/workerenv/build.sh --native   # directly, on an EL9 x86_64 host such as the AF login node
#
# Writes phase0/workerenv/dist/worker-env.tar.gz and worker-env.tar.gz.sha256.
# The tarball unpacks to worker-env/{python,app}; run python as worker-env/python/bin/python3
# with PYTHONPATH=worker-env/app.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
phase0=$(dirname "$here")
python_version=3.12
el_image=almalinux:9

if [[ ${1:-} != --native ]]; then
  exec docker run --rm --platform linux/amd64 \
    -v "$phase0":/phase0:ro -v "$here/dist":/dist \
    -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
    "$el_image" bash /phase0/workerenv/build.sh --native /phase0 /dist
fi

src=${2:-$phase0}
dist=${3:-$here/dist}
mkdir -p "$dist"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

[[ $(uname -m) == x86_64 ]] || { echo "build on x86_64, not $(uname -m)" >&2; exit 1; }

# uv only to fetch python-build-standalone and install wheels; it is not shipped
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$work/uv" UV_NO_MODIFY_PATH=1 sh >/dev/null
uv="$work/uv/uv"

env="$work/worker-env"
mkdir -p "$env/app"
export UV_PYTHON_INSTALL_DIR="$work/pythons"
"$uv" python install --quiet "$python_version"
# uv links cpython-3.12-<platform> to the patch release; copy the real directory
python_bin=$(realpath "$("$uv" python find "$python_version")")
cp -a "$(dirname "$(dirname "$python_bin")")" "$env/python"
rm -f "$env"/python/lib/python3*/EXTERNALLY-MANAGED

"$uv" pip install --quiet --python "$env/python/bin/python3" --no-deps --require-hashes \
  -r <(cd "$src/workerenv" && "$uv" pip compile --quiet --generate-hashes --no-header \
        --python-version "$python_version" --python-platform x86_64-manylinux_2_28 requirements.txt)
cp -a "$src/shim" "$env/app/shim"
find "$env" -name __pycache__ -prune -exec rm -rf {} +

# Smoke test before packing
PYTHONPATH="$env/app" "$env/python/bin/python3" -c 'import temporalio, uproot, shim.noop_worker; print("imports ok:", temporalio.__name__, uproot.__version__)'

tar -C "$work" -czf "$dist/worker-env.tar.gz" worker-env
(cd "$dist" && sha256sum worker-env.tar.gz > worker-env.tar.gz.sha256)
[[ -n ${HOST_UID:-} ]] && chown "$HOST_UID:${HOST_GID:-$HOST_UID}" "$dist"/worker-env.tar.gz*
echo "$(du -h "$dist/worker-env.tar.gz" | cut -f1) $(cat "$dist/worker-env.tar.gz.sha256")"
