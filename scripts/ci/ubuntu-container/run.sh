#!/usr/bin/env bash
# Reproduce the hosted `ci (ubuntu-latest)` job locally in a Linux container.
#
#   scripts/ci/ubuntu-container/run.sh [pytest args...]
#   scripts/ci/ubuntu-container/run.sh --shell
#   scripts/ci/ubuntu-container/run.sh --rebuild [pytest args...]
#
# Builds the image (tag traceback-ci-ubuntu, linux/amd64) when it is missing,
# mounts this checkout read-write at /work, runs `uv sync --frozen` into a
# throwaway environment, then pytest with the given arguments. The host .venv
# and uv cache are never touched. The pytest exit code is passed through.
# Default pytest arguments when none are given:
#   -q -p no:cacheprovider -n 0 --timeout 600 -m "not slow"
# See docs/DEVELOPMENT-CONTROL.md, "Reproducing an Ubuntu-only failure".
set -euo pipefail

IMAGE="traceback-ci-ubuntu"
PLATFORM="linux/amd64"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECKOUT="$(cd "$HERE/../../.." && pwd)"

mode="pytest"
rebuild=0
while [ $# -gt 0 ]; do
    case "$1" in
        --shell) mode="shell"; shift ;;
        --rebuild) rebuild=1; shift ;;
        --help|-h)
            sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) break ;;
    esac
done

echo "traceback-ci-ubuntu: reproduction aid only; the hosted ci (ubuntu-latest) job is the gate." >&2

if [ "$rebuild" -eq 1 ] || ! docker image inspect "$IMAGE" > /dev/null 2>&1; then
    echo "Building $IMAGE for $PLATFORM from $HERE/Dockerfile" >&2
    docker build --platform "$PLATFORM" -t "$IMAGE" "$HERE"
fi

# stdin is always attached (so `--shell` accepts a piped script); a tty is
# allocated only when the caller has one.
tty_flags=(-i)
if [ -t 0 ] && [ -t 1 ]; then
    tty_flags=(-it)
fi

docker_run=(
    docker run --rm --init "${tty_flags[@]}"
    --platform "$PLATFORM"
    -v "$CHECKOUT:/work"
    -w /work
    -e CHROME_BIN=/usr/local/bin/chrome-wrapper.sh
    -e UV_PROJECT_ENVIRONMENT=/tmp/venv
    -e UV_CACHE_DIR=/tmp/uvcache
    -e PYTHONDONTWRITEBYTECODE=1
    "$IMAGE"
)

if [ "$mode" = "shell" ]; then
    exec "${docker_run[@]}" bash -c 'uv sync --frozen && exec bash'
fi

if [ $# -eq 0 ]; then
    set -- -q -p no:cacheprovider -n 0 --timeout 600 -m "not slow"
fi

set +e
"${docker_run[@]}" bash -c 'uv sync --frozen && exec uv run --frozen pytest -p no:cacheprovider "$@"' pytest "$@"
status=$?
set -e
echo "traceback-ci-ubuntu: pytest exit $status (reproduction aid; not the hosted gate)" >&2
exit "$status"
