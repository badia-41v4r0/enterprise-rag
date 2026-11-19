#!/bin/sh
# End-to-end test of a company installation from scratch (PLAN-v0.4 section 9), on a THROW-AWAY installation:
# its own Compose project, volumes, image tag, ports and runner socket directory. Never another installation's.
#
#   1. `erag init` writes a fresh environment file (production settings, random secrets, master key, setup token)
#   2. the stack starts from an empty database (setup mode)
#   3. scripts/setup_e2e.mjs drives the wizard in a real browser, finishes it, and logs in as two users with
#      different roles who get different answers
#   4. the project is removed with `down -v` (set KEEP=1 to keep it running)
#
#   NODE_PATH=.scratch/e2e/node_modules CHROMIUM=/usr/bin/chromium scripts/setup_e2e.sh
#
# Variables: PROJECT (erag-test-setup), APP_PORT (8099), PG_PORT (55499), RUNNER_PORT (8797), OFFICE_PORT (8798),
# KEEP (0), SKIP_BUILD (0).
set -eu
cd "$(dirname "$0")/.."
PROJECT=${PROJECT:-erag-test-setup}
case "$PROJECT" in erag-test-*) ;; *) echo "PROJECT must start with erag-test- (throw-away only)" >&2; exit 2 ;; esac
DIR=.scratch/$PROJECT
APP_PORT=${APP_PORT:-8099}
PG_PORT=${PG_PORT:-55499}
export ERAG_IMAGE=${ERAG_IMAGE:-$PROJECT:dev}
export ERAG_RUNNER_SOCKET_DIR=/tmp/$PROJECT-runner-sockets
export THROWAWAY_RUNNER_PORT=${RUNNER_PORT:-8797}
export THROWAWAY_OFFICE_PORT=${OFFICE_PORT:-8798}
COMPOSE="docker compose -p $PROJECT --env-file $DIR/.env -f deploy/docker-compose.yml -f deploy/docker-compose.throwaway.yml"

if [ -f "$DIR/.env" ]; then
  $COMPOSE down -v >/dev/null 2>&1 || true
fi
rm -rf "$DIR"
mkdir -p "$DIR"
chmod 700 "$DIR"
umask 077

echo "== erag init"
# A database URL that cannot connect: erag init must not need (or touch) any database.
ERAG_DATABASE_URL=postgresql+asyncpg://none:none@127.0.0.1:1/none \
  uv run erag init --env-file "$DIR/.env" --app-port "$APP_PORT" --postgres-port "$PG_PORT" > "$DIR/init.out"
grep -v -E '^    [A-Z0-9]{4}(-[A-Z0-9]{4}){5}$' "$DIR/init.out"
TOKEN=$(grep -E '^    [A-Z0-9]{4}(-[A-Z0-9]{4}){5}$' "$DIR/init.out" | tr -d ' ')
[ -n "$TOKEN" ] || { echo "no setup token printed" >&2; exit 1; }
stat -c '%a %n' "$DIR/.env" "$DIR/master.key"

echo "== start the throw-away stack ($PROJECT, app on 127.0.0.1:$APP_PORT, postgres on $PG_PORT)"
if [ "${SKIP_BUILD:-0}" = "1" ]; then
  $COMPOSE up -d
else
  $COMPOSE up -d --build
fi
i=0
until curl -fsS "http://127.0.0.1:$APP_PORT/health" >/dev/null 2>&1; do
  i=$((i + 1))
  [ $i -lt 120 ] || { $COMPOSE logs app | tail -50; exit 1; }
  sleep 2
done
echo "== app is up; bootstrap log:"
$COMPOSE logs app 2>/dev/null | grep -E "SETUP MODE|Setup token|erag init" | sed -E 's/[A-Z0-9]{4}(-[A-Z0-9]{4}){5}/<token>/'

status=0
SETUP_TOKEN="$TOKEN" BASE="http://127.0.0.1:$APP_PORT" OUT="${OUT:-.scratch}" node scripts/setup_e2e.mjs || status=$?

if [ "${KEEP:-0}" != "1" ]; then
  echo "== removing the throw-away project $PROJECT (down -v)"
  $COMPOSE down -v
  rm -rf "$DIR" "$ERAG_RUNNER_SOCKET_DIR" 2>/dev/null || true
fi
exit $status
