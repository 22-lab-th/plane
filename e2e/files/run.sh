#!/usr/bin/env bash
# Files tab end-to-end harness (T-112).
#
# Brings up Postgres, Valkey and MinIO, migrates and seeds through the API, starts the
# API and the web app, runs the Playwright spec, and tears everything down on exit.
#
# There is no host virtualenv in this checkout, so every Django command runs in the
# prebuilt `plane_api-tests` image (the same image the pytest suite uses). The API
# container joins the compose network so it can reach Postgres, Valkey and MinIO by
# container name, while the browser-facing MinIO URL is published on 127.0.0.1.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_PROJECT="${E2E_COMPOSE_PROJECT:-plane-files-e2e}"
STATE_DIR="$ROOT_DIR/.e2e-files"
COMPOSE_FILE="$ROOT_DIR/docker-compose-test.yml"
IMAGE="${E2E_API_IMAGE:-localhost/plane_api-tests:latest}"
NETWORK="${COMPOSE_PROJECT}_test_env"
API_CONTAINER="${COMPOSE_PROJECT}-api"
MINIO_CONTAINER="${COMPOSE_PROJECT}-minio"
MQ_CONTAINER="${COMPOSE_PROJECT}-mq"
MQ_PORT=59110
MINIO_PORT=59010
MINIO_CONSOLE_PORT=59011
API_PORT="${E2E_API_PORT:-58000}"
WEB_PORT="${E2E_WEB_PORT:-53000}"
WEB_URL="http://127.0.0.1:${WEB_PORT}"
API_URL="http://127.0.0.1:${API_PORT}"
PIDS=()
compose() { podman compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" "$@"; }

cleanup() {
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  podman rm -f "$API_CONTAINER" "$MINIO_CONTAINER" "$MQ_CONTAINER" >/dev/null 2>&1 || true
  compose down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

cd "$ROOT_DIR"
if ! podman image exists "$IMAGE"; then
  echo "Missing $IMAGE. Build with: podman compose -p plane -f docker-compose-test.yml build api-tests" >&2
  exit 1
fi
rm -rf "$STATE_DIR"
mkdir -p "$STATE_DIR"
# Clean only this harness's explicitly named containers.
podman rm -f "$API_CONTAINER" "$MINIO_CONTAINER" "$MQ_CONTAINER" >/dev/null 2>&1 || true

say() { echo "[files-e2e $(date +%H:%M:%S)] $*"; }

compose down -v >/dev/null 2>&1 || true
compose up -d test-db test-redis >"$STATE_DIR/compose.log" 2>&1
say "compose up: $(podman network ls --format '{{.Name}}' | grep -c "^${NETWORK}$") network(s)"

podman run --rm -d --name "$MINIO_CONTAINER" --network "$NETWORK" \
  -e MINIO_ROOT_USER=access-key -e MINIO_ROOT_PASSWORD=secret-key \
  -p "127.0.0.1:${MINIO_PORT}:9000" -p "127.0.0.1:${MINIO_CONSOLE_PORT}:9090" \
  --tmpfs /data:rw,mode=1777 \
  minio/minio server /data --address ':9000' --console-address ':9090' >/dev/null 2>>"$STATE_DIR/minio.log"
ready=""
for _ in {1..60}; do
  if podman exec "$MINIO_CONTAINER" /bin/sh -c \
    "mc alias set local http://localhost:9000 access-key secret-key >/dev/null 2>&1"; then ready=1; break; fi
  sleep 1
done
if [[ -z "$ready" ]]; then
  say "MinIO did not become ready"
  podman logs "$MINIO_CONTAINER" >>"$STATE_DIR/minio.log" 2>&1 || true
  podman ps -a --format '{{.Names}} {{.Status}}' >>"$STATE_DIR/minio.log" 2>&1 || true
  tail -20 "$STATE_DIR/minio.log" >&2
  exit 1
fi
podman exec "$MINIO_CONTAINER" /bin/sh -c "mc mb local/uploads -p" >>"$STATE_DIR/minio.log" 2>&1 || true
say "minio ready at http://127.0.0.1:${MINIO_PORT}"

podman run --rm -d --name "$MQ_CONTAINER" --network "$NETWORK" \
  -e RABBITMQ_DEFAULT_USER=plane -e RABBITMQ_DEFAULT_PASS=plane -e RABBITMQ_DEFAULT_VHOST=plane \
  --tmpfs /var/lib/rabbitmq:rw,mode=1777 \
  -p "127.0.0.1:${MQ_PORT}:5672" \
  rabbitmq:3.13.6-management-alpine >/dev/null 2>>"$STATE_DIR/mq.log"

# Every dependency is checked before anything is started on top of it: a broker that is
# down makes endpoints that enqueue Celery work answer 500, and a harness that proceeds
# anyway turns that into confusing failures much later.
wait_for_dependency() {
  local name="$1" command="$2" tries="${3:-60}"
  shift 3
  for _ in $(seq 1 "$tries"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
  say "dependency unhealthy: $name ($command)"
  podman logs "$name" >>"$STATE_DIR/deps.log" 2>&1 || true
  compose logs test-db test-redis >>"$STATE_DIR/deps.log" 2>&1 || true
  tail -25 "$STATE_DIR/deps.log" >&2
  exit 1
}
# Probed over a published port with a client the harness already needs: an in-container
# probe would depend on which CLI the image happens to ship, and a readiness check that
# fails on the probe rather than on the broker is worse than no check at all.
mq_ready() { timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/${MQ_PORT}" >/dev/null 2>&1; }
wait_for_dependency "$MQ_CONTAINER" "amqp port ${MQ_PORT}" 120 mq_ready
# The compose health status, not `pg_isready`: Postgres's temporary initdb server answers
# pg_isready and is then shut down, so a probe it satisfies can pass while the database the
# API will talk to is not there yet.
wait_for_health() {
  local container="$1" tries="${2:-90}" status=""
  for _ in $(seq 1 "$tries"); do
    status="$(podman inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || echo missing)"
    [[ "$status" == "healthy" ]] && { say "$container healthy"; return 0; }
    sleep 1
  done
  say "dependency unhealthy: $container (state: $status)"
  podman logs "$container" >>"$STATE_DIR/deps.log" 2>&1 || true
  compose logs test-db test-redis >>"$STATE_DIR/deps.log" 2>&1 || true
  tail -25 "$STATE_DIR/deps.log" >&2
  exit 1
}
wait_for_health "${COMPOSE_PROJECT}_test-db_1"
wait_for_health "${COMPOSE_PROJECT}_test-redis_1"
say "broker and stores ready"

# Django-side environment shared by every container command below.
DJANGO_ENV=(
  -e DJANGO_SETTINGS_MODULE=plane.settings.local
  # The suite signs in about twenty times (one per test plus the guest contexts) and the
  # product default is AUTHENTICATION_RATE_LIMIT=10/minute per IP. Without this, the sign-ins
  # that fall past the budget answer 302 RATE_LIMIT_EXCEEDED, no session is established, and
  # whichever test they belong to fails on some unrelated assertion much later (DEFECT-007).
  # The product's own default is untouched; only this harness's stack is raised.
  -e AUTHENTICATION_RATE_LIMIT=300/minute
  -e PROJECT_FILE_UPLOAD_RATE_LIMIT=1000/minute
  -e DATABASE_URL=postgresql://plane:plane@test-db:5432/plane
  -e REDIS_URL=redis://test-redis:6379/
  -e RABBITMQ_HOST="$MQ_CONTAINER"
  -e RABBITMQ_PORT=5672
  -e RABBITMQ_USER=plane
  -e RABBITMQ_PASSWORD=plane
  -e RABBITMQ_VHOST=plane
  -e SECRET_KEY=plane-files-e2e-secret-key-local-only
  -e WEB_URL="$WEB_URL"
  -e APP_BASE_URL="$WEB_URL"
  -e ADMIN_BASE_URL=http://127.0.0.1:3001
  -e CORS_ALLOWED_ORIGINS="$WEB_URL"
  -e CSRF_TRUSTED_ORIGINS="$WEB_URL"
  -e DEBUG=1
  -e EMAIL_HOST=localhost
  -e AWS_ACCESS_KEY_ID=access-key
  -e AWS_SECRET_ACCESS_KEY=secret-key
  -e AWS_S3_BUCKET_NAME=uploads
  -e AWS_S3_ENDPOINT_URL="http://${MINIO_CONTAINER}:9000"
  -e MINIO_PUBLIC_ENDPOINT_URL="http://127.0.0.1:${MINIO_PORT}"
  -e AWS_S3_REGION_NAME=us-east-1
  -e USE_MINIO=1
)

fail() { say "FAILED: $1"; tail -25 "$2" >&2 || true; exit 1; }

podman run --rm --network "$NETWORK" "${DJANGO_ENV[@]}" -v "$ROOT_DIR/apps/api:/code" -w /code \
  --entrypoint python "$IMAGE" manage.py migrate \
  --settings=plane.settings.local --noinput >"$STATE_DIR/migrate.log" 2>&1 || fail "migrate" "$STATE_DIR/migrate.log"
say "migrated"
podman run --rm -i --network "$NETWORK" "${DJANGO_ENV[@]}" -v "$ROOT_DIR/apps/api:/code" -w /code \
  --entrypoint python "$IMAGE" manage.py shell --settings=plane.settings.local \
  <e2e/files/seed.py >"$STATE_DIR/seed.log" 2>&1 || fail "db seed" "$STATE_DIR/seed.log"
grep -E '^E2E_(WORKSPACE_SLUG|PROJECT_ID|ISSUE_UNLINK_ID|ISSUE_UPLOAD_ID)=' "$STATE_DIR/seed.log" >"$STATE_DIR/ids.env"
# shellcheck disable=SC1091
source "$STATE_DIR/ids.env"
say "seeded workspace $E2E_WORKSPACE_SLUG project $E2E_PROJECT_ID"

podman run --rm -d --name "$API_CONTAINER" --network "$NETWORK" -p "127.0.0.1:${API_PORT}:8000" \
  "${DJANGO_ENV[@]}" -v "$ROOT_DIR/apps/api:/code" -w /code \
  --entrypoint python "$IMAGE" manage.py runserver "0.0.0.0:8000" \
  --settings=plane.settings.local >"$STATE_DIR/api.log" 2>&1

VITE_API_BASE_URL="$API_URL" VITE_WEB_BASE_URL="$WEB_URL" VITE_ADMIN_BASE_URL="http://127.0.0.1:3001" \
  pnpm --filter=web exec react-router dev --port "$WEB_PORT" >"$STATE_DIR/web.log" 2>&1 &
PIDS+=("$!")

wait_for_url() {
  local url="$1"
  for _ in {1..180}; do curl -ksSf "$url" >/dev/null 2>&1 && return 0; sleep 1; done
  echo "Timed out waiting for $url" >&2
  tail -40 "$STATE_DIR/api.log" "$STATE_DIR/web.log" >&2 || true
  return 1
}

wait_for_url "${API_URL}/api/instances/"
wait_for_url "${WEB_URL}/"

# The folder tree and the files are created through the API, so the fixture is real.
# This runs on the host: the presigned URL points at the published MinIO port, which
# is reachable as 127.0.0.1 from here but not from inside the API container.
E2E_API_URL="$API_URL" \
E2E_WEB_URL="$WEB_URL" \
E2E_WORKSPACE_SLUG="$E2E_WORKSPACE_SLUG" \
E2E_PROJECT_ID="$E2E_PROJECT_ID" \
E2E_EMAIL=files.e2e@example.com \
E2E_PASSWORD='PlaneE2E!Files123' \
  python3 e2e/files/seed_content.py >"$STATE_DIR/seed_content.log" 2>&1 ||
  fail "content seed" "$STATE_DIR/seed_content.log"
say "seeded folders and files through the API"

export E2E_API_URL="$API_URL"
export E2E_WEB_URL="$WEB_URL"
export E2E_EMAIL=files.e2e@example.com
export E2E_PASSWORD='PlaneE2E!Files123'
export E2E_GUEST_EMAIL=files.guest.e2e@example.com
export E2E_GUEST_PASSWORD='PlaneE2E!Guest123'
export E2E_WORKSPACE_SLUG
export E2E_PROJECT_ID
export E2E_ISSUE_UNLINK_ID
export E2E_ISSUE_UPLOAD_ID

# The object store as the browser reaches it, so a spec can read the bytes an upload
# landed rather than only the row that claims they exist. The credentials are the ones
# the MinIO container was started with, and the bucket is the one created above.
export E2E_MINIO_URL="http://127.0.0.1:${MINIO_PORT}"
export E2E_MINIO_BUCKET=uploads
export E2E_MINIO_ACCESS_KEY=access-key
export E2E_MINIO_SECRET_KEY=secret-key
export E2E_MINIO_REGION=us-east-1

if [[ "${E2E_FILES_KEEP_STACK:-}" == "1" ]]; then
  say "stack kept up: api $API_URL web $WEB_URL minio http://127.0.0.1:${MINIO_PORT} — interrupt to tear down"
  while true; do sleep 30; done
fi

pnpm exec playwright test --config=playwright.files.config.ts "$@"
