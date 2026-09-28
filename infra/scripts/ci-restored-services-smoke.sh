#!/usr/bin/env bash
set -Eeuo pipefail

repository_root="$(cd "$(dirname "$0")/../.." && pwd)"
restore_container="${1:?restored PostgreSQL container is required}"
backend_image="${2:-vlytics-backend:local}"
read_password="${VLYTICS_RESTORED_READ_PASSWORD:?VLYTICS_RESTORED_READ_PASSWORD is required}"
engine_password="${VLYTICS_RESTORED_ENGINE_PASSWORD:?VLYTICS_RESTORED_ENGINE_PASSWORD is required}"
operator_token="ci-restored-operator-secret"
container_suffix="$$-$RANDOM"
api_name="vlytics-restored-api-$container_suffix"
worker_name="vlytics-restored-worker-$container_suffix"
api_id=""
worker_id=""

[[ "$restore_container" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]
[[ "$backend_image" != -* && -n "$backend_image" ]]
[[ "$read_password" =~ ^[A-Za-z0-9._~-]+$ ]]
[[ "$engine_password" =~ ^[A-Za-z0-9._~-]+$ ]]
test "$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$restore_container")" = none

after_cleanup() {
  local result=$?
  trap - EXIT
  if (( result != 0 )); then
    if [[ -n "$api_id" ]]; then docker logs --tail=80 "$api_id" || true; fi
    if [[ -n "$worker_id" ]]; then docker logs --tail=80 "$worker_id" || true; fi
  fi
  if [[ -n "$worker_id" ]]; then
    docker rm --force "$worker_id" > /dev/null 2>&1 || true
  fi
  if [[ -n "$api_id" ]]; then
    docker rm --force "$api_id" > /dev/null 2>&1 || true
  fi
  exit "$result"
}
trap after_cleanup EXIT

restored_query() {
  docker exec "$restore_container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U postgres -d vlytics -c "$1" | tr -d '\r'
}

job_state_query="SELECT state || ':' || attempt_no::text || ':' ||
  (SELECT count(*)::text FROM ops.job_attempts WHERE job_id = jobs.id)
  FROM ops.jobs AS jobs WHERE job_key = 'ci-disabled-source';"
test "$(restored_query "$job_state_query")" = "quarantined:1:1"

read_database_url="postgresql+psycopg://vlytics_read_api_login:${read_password}@127.0.0.1:5432/vlytics"
engine_database_url="postgresql+psycopg://vlytics_engine_login:${engine_password}@127.0.0.1:5432/vlytics"

api_id="$(docker run --pull never --detach --name "$api_name" \
  --network "container:$restore_container" \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,uid=65532,gid=65532 \
  --volume "$repository_root/config/example.toml:/app/config/example.toml:ro" \
  --volume "$repository_root/contracts/config.schema.json:/app/contracts/config.schema.json:ro" \
  --env VLYTICS_DATABASE_URL="$read_database_url" \
  --env VLYTICS_OPERATOR_AUTH_SECRET="$operator_token" \
  --env VLYTICS_READONLY_AUTH_SECRET=ci-restored-readonly-secret \
  --env VLYTICS_OPERATIONAL_CONFIG_PATH=/app/config/example.toml \
  --env VLYTICS_OPERATIONAL_SCHEMA_PATH=/app/contracts/config.schema.json \
  "$backend_image" vlytics-api)"

api_ready=false
for attempt in $(seq 1 30); do
  if docker exec "$api_id" python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()" \
    > /dev/null 2>&1; then
    api_ready=true
    break
  fi
  sleep 1
done
test "$api_ready" = true

docker exec --env RESTORED_OPERATOR_TOKEN="$operator_token" -i "$api_id" python - <<'PY'
import json
import os
import urllib.error
import urllib.request

base = "http://127.0.0.1:8000"
with urllib.request.urlopen(base + "/health", timeout=5) as response:
    health = json.load(response)
    assert response.status == 200
    assert health["status"] == "ok"
    assert health["service"] == "vlytics-api"

schedule = base + "/api/v1/schedule?date=2026-09-25&timezone=Asia%2FSeoul"
try:
    urllib.request.urlopen(schedule, timeout=5)
except urllib.error.HTTPError as error:
    assert error.code == 401
else:
    raise AssertionError("anonymous restored schedule request must be rejected")

request = urllib.request.Request(
    schedule,
    headers={"Authorization": "Bearer " + os.environ["RESTORED_OPERATOR_TOKEN"]},
)
with urllib.request.urlopen(request, timeout=5) as response:
    payload = json.load(response)
    assert response.status == 200
    assert payload["metadata"]["schema_version"] == "vlytics.operator.v1"
    assert isinstance(payload["data"]["items"], list)
PY

worker_id="$(docker run --pull never --detach --name "$worker_name" \
  --network "container:$restore_container" \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,uid=65532,gid=65532 \
  --volume "$repository_root/config/example.toml:/app/config/example.toml:ro" \
  --volume "$repository_root/contracts/config.schema.json:/app/contracts/config.schema.json:ro" \
  --env VLYTICS_DATABASE_URL="$engine_database_url" \
  --env VLYTICS_OPERATIONAL_CONFIG_PATH=/app/config/example.toml \
  --env VLYTICS_OPERATIONAL_SCHEMA_PATH=/app/contracts/config.schema.json \
  --env VLYTICS_WORKER_HEARTBEAT_PATH=/tmp/vlytics-worker-heartbeat.json \
  "$backend_image" vlytics-worker)"

worker_polled=false
for attempt in $(seq 1 30); do
  if docker exec "$worker_id" python -m vlytics.ops.heartbeat \
    --path /tmp/vlytics-worker-heartbeat.json --max-age-seconds 60; then
    worker_polled=true
    break
  fi
  sleep 1
done
test "$worker_polled" = true
test "$(restored_query "$job_state_query")" = "quarantined:1:1"

echo "Restored API and worker entrypoints passed against the isolated restored database."