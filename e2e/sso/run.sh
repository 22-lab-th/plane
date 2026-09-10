#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE_DIR="$ROOT_DIR/.e2e-sso"
DB_CONTAINER="plane-sso-e2e-db"
CACHE_CONTAINER="plane-sso-e2e-cache"
PIDS=()

cleanup() {
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  podman rm -f "$DB_CONTAINER" "$CACHE_CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

cd "$ROOT_DIR"
rm -rf "$STATE_DIR"
mkdir -p "$STATE_DIR"
podman rm -f "$DB_CONTAINER" "$CACHE_CONTAINER" >/dev/null 2>&1 || true
podman run --rm -d --name "$DB_CONTAINER" -e POSTGRES_USER=plane -e POSTGRES_PASSWORD=plane -e POSTGRES_DB=plane -p 55433:5432 postgres:15.7-alpine >/dev/null
podman run --rm -d --name "$CACHE_CONTAINER" -p 56380:6379 valkey/valkey:7.2.11-alpine >/dev/null

for _ in {1..30}; do podman exec "$DB_CONTAINER" pg_isready -U plane -d plane >/dev/null 2>&1 && break; sleep 1; done
for _ in {1..30}; do podman exec "$CACHE_CONTAINER" valkey-cli ping >/dev/null 2>&1 && break; sleep 1; done
podman exec "$DB_CONTAINER" pg_isready -U plane -d plane >/dev/null
podman exec "$CACHE_CONTAINER" valkey-cli ping >/dev/null

openssl req -x509 -newkey rsa:2048 -nodes -days 1 -keyout "$STATE_DIR/idp.key" -out "$STATE_DIR/idp.crt" -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1

export DATABASE_URL="postgresql://plane:plane@127.0.0.1:55433/plane"
export REDIS_URL="redis://127.0.0.1:56380/"
export SECRET_KEY="plane-sso-e2e-secret-key-local-only"
export WEB_URL="http://127.0.0.1:3000"
export APP_BASE_URL="$WEB_URL"
export ADMIN_BASE_URL="http://127.0.0.1:3001"
export CORS_ALLOWED_ORIGINS="http://127.0.0.1:3000,http://127.0.0.1:3001"
export OIDC_ALLOWED_IPS="127.0.0.1/32"
export OIDC_CA_BUNDLE="$STATE_DIR/idp.crt"
export SSO_BREAK_GLASS_ADMIN_EMAILS="recovery.e2e@example.com"
export DEBUG=1
export EMAIL_HOST=localhost

apps/api/.venv/bin/python apps/api/manage.py migrate --settings=plane.settings.local --noinput >"$STATE_DIR/migrate.log" 2>&1
apps/api/.venv/bin/python apps/api/manage.py shell --settings=plane.settings.local <e2e/sso/seed.py >"$STATE_DIR/seed.log" 2>&1

apps/api/.venv/bin/python e2e/sso/mock_idp.py --cert "$STATE_DIR/idp.crt" --key "$STATE_DIR/idp.key" >"$STATE_DIR/idp.log" 2>&1 &
PIDS+=("$!")
apps/api/.venv/bin/python apps/api/manage.py runserver 127.0.0.1:8000 --settings=plane.settings.local >"$STATE_DIR/api.log" 2>&1 &
PIDS+=("$!")
VITE_API_BASE_URL="http://127.0.0.1:8000" VITE_WEB_BASE_URL="$WEB_URL" VITE_ADMIN_BASE_URL="$ADMIN_BASE_URL" pnpm --filter=web dev >"$STATE_DIR/web.log" 2>&1 &
PIDS+=("$!")
VITE_API_BASE_URL="http://127.0.0.1:8000" VITE_WEB_BASE_URL="$WEB_URL" VITE_ADMIN_BASE_URL="$ADMIN_BASE_URL" VITE_ADMIN_BASE_PATH="/god-mode" pnpm --filter=admin dev >"$STATE_DIR/admin.log" 2>&1 &
PIDS+=("$!")

wait_for_url() {
  local url="$1"
  for _ in {1..90}; do curl -ksSf "$url" >/dev/null 2>&1 && return 0; sleep 1; done
  echo "Timed out waiting for $url" >&2
  return 1
}

wait_for_url "https://127.0.0.1:9443/health"
wait_for_url "http://127.0.0.1:8000/api/instances/"
wait_for_url "http://127.0.0.1:3000/"
wait_for_url "http://127.0.0.1:3001/god-mode/"

pnpm exec playwright test --config=playwright.sso.config.ts
