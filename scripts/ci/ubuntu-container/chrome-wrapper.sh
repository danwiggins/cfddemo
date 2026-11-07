#!/bin/sh
# Headless Chrome inside the reproduction container. The test harness honours
# $CHROME_BIN (tests/lab/test_lab_server.py, _chrome); run.sh points it here.
# --no-sandbox: the container runs as root without user namespaces.
# --disable-gpu: no GPU in the container.
# --disable-dev-shm-usage: /dev/shm is 64 MB by default under Docker.
exec /usr/bin/google-chrome --no-sandbox --disable-gpu --disable-dev-shm-usage "$@"
