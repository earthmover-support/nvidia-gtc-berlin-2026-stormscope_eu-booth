#!/bin/bash
# Setup script for the StormScope-EU Brev launchable (VM Mode). Paste it into the
# launchable's "Setup script" field; see docs/BREV.md.
#
# Brev passes the launchable's setup values as environment variables to this script only,
# and deletes them after setup. The script therefore:
#   1. clones this repo to /opt/stormscope-eu at STORMSCOPE_GIT_REF,
#   2. writes the credentials to /opt/stormscope-eu/secrets/ (root, mode 600) and the
#      other values to /opt/stormscope-eu/.env, which compose.yaml reads,
#   3. runs `docker compose up -d --build`; the service's `restart: unless-stopped`
#      restarts the pipeline after a crash or a reboot.
#
# Setup values:
#   EUMETSAT_CONSUMER_KEY      required
#   EUMETSAT_CONSUMER_SECRET   required
#   ARRAYLAKE_API_TOKEN        write token; required unless STORMSCOPE_PUBLISH_TARGET=local
#   ARRAYLAKE_REPO             <org>/<repo>; required unless STORMSCOPE_PUBLISH_TARGET=local
#   STORMSCOPE_PUBLISH_TARGET  arraylake (default) or local
#   STORMSCOPE_REALTIME        0 (default) or 1 (EUMETSAT account holds the NRT licence)
#   STORMSCOPE_GIT_REF         branch, tag or commit of this repo; default main
#
# Rerunning the script (with new values) updates the checkout, secrets and .env, and
# recreates the container. Do not add `set -x`: it would print the secrets to the setup log.
set -euo pipefail
step() { echo "[brev-setup] $(date -u +%H:%M:%S) $*"; }
die() { echo "[brev-setup] $*" >&2; exit 1; }

GIT_URL=https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth.git
GIT_REF=${STORMSCOPE_GIT_REF:-main}
APP_DIR=/opt/stormscope-eu
PUBLISH_TARGET=${STORMSCOPE_PUBLISH_TARGET:-arraylake}
REALTIME=${STORMSCOPE_REALTIME:-0}

SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

[ -n "${EUMETSAT_CONSUMER_KEY:-}" ] || die "EUMETSAT_CONSUMER_KEY is empty"
[ -n "${EUMETSAT_CONSUMER_SECRET:-}" ] || die "EUMETSAT_CONSUMER_SECRET is empty"
case "$PUBLISH_TARGET" in
    arraylake)
        [ -n "${ARRAYLAKE_API_TOKEN:-}" ] || die "ARRAYLAKE_API_TOKEN is empty"
        [ -n "${ARRAYLAKE_REPO:-}" ] || die "ARRAYLAKE_REPO is empty; set it to <org>/<repo>" ;;
    local) ;;
    *) die "STORMSCOPE_PUBLISH_TARGET must be arraylake or local" ;;
esac
case "$REALTIME" in 0|1) ;; *) die "STORMSCOPE_REALTIME must be 0 or 1" ;; esac
for v in "${ARRAYLAKE_REPO:-}" "$GIT_REF"; do
    case "$v" in *$'\n'*|*$'\r'*) die "setup values must be one line" ;; esac
done

step "host GPU"
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
# The NGC PhysicsNeMo 26.08 image (CUDA 13.2) needs driver 610+, or 580/595 with CUDA
# forward compatibility. Driver 565 (Crusoe's L40S image) has neither, and the model then
# has no GPU.
driver=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1 | cut -d. -f1)
[ "$driver" -ge 580 ] 2>/dev/null \
    || die "NVIDIA driver $driver is too old for the CUDA 13.2 container; pick another provider (e.g. Nebius)"
command -v docker >/dev/null || die "docker not found; use VM Mode"
$SUDO docker compose version >/dev/null 2>&1 || die "docker compose plugin not found"
$SUDO docker info --format '{{json .Runtimes}}' | grep -q nvidia \
    || echo "[brev-setup] WARNING: docker lists no nvidia runtime; the GPU reservation may fail"
# Some Brev images (seen on Crusoe L40S, toolkit 1.20.1, driver 565) set the NVIDIA runtime
# to CDI mode without a CDI spec, so every GPU container fails with "unresolvable CDI devices
# nvidia.com/gpu=all", and `nvidia-ctk cdi generate` crashes there. Generate the spec; if
# that fails, switch the runtime to legacy mode.
if command -v nvidia-ctk >/dev/null && ! ls /etc/cdi/*.yaml /var/run/cdi/*.yaml >/dev/null 2>&1 \
    && grep -q '^mode = "cdi"' /etc/nvidia-container-runtime/config.toml 2>/dev/null; then
    step "no CDI spec: generate one, else use the legacy NVIDIA runtime mode"
    if ! $SUDO nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml >/dev/null 2>&1; then
        $SUDO rm -f /etc/cdi/nvidia.yaml
        $SUDO nvidia-ctk config --set nvidia-container-runtime.mode=legacy --in-place
    fi
fi

step "checkout $GIT_REF -> $APP_DIR"
$SUDO mkdir -p "$APP_DIR"
[ -d "$APP_DIR/.git" ] || $SUDO git -C "$APP_DIR" init -q
$SUDO git -C "$APP_DIR" fetch -q --depth 1 "$GIT_URL" "$GIT_REF"
$SUDO git -C "$APP_DIR" checkout -q --force FETCH_HEAD
$SUDO git -C "$APP_DIR" log -1 --format='[brev-setup] commit %h %s'

step "secrets and .env"
put() {  # put <file> <content>: write root-only, without the content on a command line
    local tmp
    tmp=$(umask 077; mktemp)
    printf '%s' "$2" > "$tmp"
    $SUDO install -D -m 600 -o root -g root "$tmp" "$1"
    rm -f "$tmp"
}
put "$APP_DIR/secrets/eumetsat_consumer_key" "$EUMETSAT_CONSUMER_KEY"
put "$APP_DIR/secrets/eumetsat_consumer_secret" "$EUMETSAT_CONSUMER_SECRET"
# compose.yaml requires the file even for a local run
put "$APP_DIR/secrets/arraylake_api_token" "${ARRAYLAKE_API_TOKEN:-}"
put "$APP_DIR/.env" "STORMSCOPE_PUBLISH_TARGET=$PUBLISH_TARGET
ARRAYLAKE_REPO=${ARRAYLAKE_REPO:-unset/unset}
STORMSCOPE_REALTIME=$REALTIME
"

step "docker compose up (first start builds the image, ~15 min)"
$SUDO docker compose --project-directory "$APP_DIR" up -d --build
step "started; logs: sudo docker compose --project-directory $APP_DIR logs -f"
