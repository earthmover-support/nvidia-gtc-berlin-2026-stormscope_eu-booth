#!/bin/sh
# Compose mounts each secret as a file under /run/secrets; the pipeline reads environment
# variables, so export each secret under its variable name. A variable already set in the
# environment wins.
set -eu

for var in EUMETSAT_CONSUMER_KEY EUMETSAT_CONSUMER_SECRET ARRAYLAKE_API_TOKEN; do
    file="/run/secrets/$(echo "$var" | tr '[:upper:]' '[:lower:]')"
    if [ -z "$(printenv "$var" || true)" ] && [ -r "$file" ]; then
        export "$var=$(cat "$file")"
    fi
done

# The NGC entrypoint sets up CUDA forward compatibility for older host drivers.
if [ -x /opt/nvidia/nvidia_entrypoint.sh ]; then
    exec /opt/nvidia/nvidia_entrypoint.sh "$@"
fi
exec "$@"
