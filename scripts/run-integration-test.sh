#!/usr/bin/env sh
set -eu

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.test.yml}"
PROJECT="${COMPOSE_PROJECT_NAME:-mail-report-test}"
ARTIFACTS="${ARTIFACTS_DIR:-artifacts}"
STATUS_FILE="$ARTIFACTS/.exit-status"

if [ -n "${CI:-}" ]; then
    FILES="-f $COMPOSE_FILE"
else
    FILES="-f $COMPOSE_FILE -f docker-compose.test.local.yml"
fi

compose() {
    docker compose -p "$PROJECT" $FILES "$@"
}

cleanup() {
    compose down -v --remove-orphans >/dev/null 2>&1 || true
}

run_logged() {
    log="$1"
    shift
    ( "$@"; echo $? > "$STATUS_FILE" ) 2>&1 | tee "$log"
    status=$(cat "$STATUS_FILE")
    rm -f "$STATUS_FILE"
    return "$status"
}

rm -rf "$ARTIFACTS"
mkdir -p "$ARTIFACTS"
cleanup
trap cleanup EXIT

set +e
run_logged "$ARTIFACTS/build.log" compose build
build_status=$?
set -e
if [ "$build_status" -ne 0 ]; then
    echo "image build failed"
    exit "$build_status"
fi

set +e
run_logged "$ARTIFACTS/compose.log" compose up --abort-on-container-exit --exit-code-from tester
status=$?
set -e

compose cp tester:/tmp/artifacts/. "$ARTIFACTS/" 2>/dev/null || echo "no artifacts produced"

echo "exit status: $status"
exit $status
