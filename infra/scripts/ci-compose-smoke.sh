#!/usr/bin/env bash
set -Eeuo pipefail

repository_root="$(cd "$(dirname "$0")/../.." && pwd)"
project="vlytics-smoke-$(date +%s)-$RANDOM"
production_project="$project-production"
restore_container="$project-restore"
frontend_container="$project-frontend"
frontend_image="$project-frontend:ci"
temporary_directory="$(mktemp -d)"

compose() {
  docker compose --project-name "$project" --file "$repository_root/infra/compose.yaml" "$@"
}

production_compose() {
  VLYTICS_BACKEND_IMAGE=vlytics-backend:local \
  VLYTICS_FRONTEND_IMAGE="$frontend_image" \
  VLYTICS_OPERATIONAL_CONFIG_FILE="$repository_root/config/example.toml" \
  VLYTICS_LIVE_DRY_RUN_EVIDENCE_FILE="$repository_root/infra/live-dry-run-evidence.example.json" \
  WEB_PORT="$VLYTICS_CI_PRODUCTION_WEB_PORT" \
    docker compose --project-name "$production_project" \
      --file "$repository_root/infra/compose.production.yaml" "$@"
}

cleanup() {
  result=$?
  trap - EXIT
  if (( result != 0 )); then
    compose ps || true
    compose logs --no-color --tail=120 postgres migrate api worker || true
    docker logs --tail=120 "$frontend_container" || true
    production_compose ps || true
    production_compose logs --no-color --tail=120 postgres migrate api worker frontend || true
  fi
  production_compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  docker rm --force "$frontend_container" >/dev/null 2>&1 || true
  docker rm --force "$restore_container" >/dev/null 2>&1 || true
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf -- "$temporary_directory"
  exit "$result"
}
trap cleanup EXIT

database_query() {
  compose exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 -U vlytics_migrator -d vlytics -c "$1" | tr -d '\r'
}

assert_running() {
  container_id="$(compose ps -q "$1")"
  test -n "$container_id"
  test "$(docker inspect --format '{{.State.Running}}' "$container_id")" = true
}

compose config --quiet
compose up --detach --build postgres migrate api worker
for attempt in $(seq 1 60); do
  if curl --fail --silent --show-error "http://127.0.0.1:$API_PORT/health" > "$temporary_directory/health.json" 2>/dev/null; then
    break
  fi
  sleep 2
done
python3 - "$temporary_directory/health.json" <<'PY'
import json
import pathlib
import sys

response = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert response["status"] == "ok"
assert response["service"] == "vlytics-api"
PY
assert_running postgres
assert_running api
assert_running worker

schedule_url="http://127.0.0.1:$API_PORT/api/v1/schedule?date=2026-09-25"
anonymous_status="$(curl --silent --output /dev/null --write-out '%{http_code}' "$schedule_url")"
test "$anonymous_status" = 401
authorized_status="$(curl --silent --output /dev/null --write-out '%{http_code}' --header "Authorization: Bearer $VLYTICS_OPERATOR_AUTH_SECRET" "$schedule_url")"
test "$authorized_status" = 200

docker pull "$VLYTICS_CI_NODE_IMAGE" > /dev/null
docker pull "$VLYTICS_CI_NGINX_IMAGE" > /dev/null
node_image="$(docker image inspect --format '{{index .RepoDigests 0}}' "$VLYTICS_CI_NODE_IMAGE")"
nginx_image="$(docker image inspect --format '{{index .RepoDigests 0}}' "$VLYTICS_CI_NGINX_IMAGE")"
[[ "$node_image" == *@sha256:* ]]
[[ "$nginx_image" == *@sha256:* ]]
docker build --file "$repository_root/frontend/Dockerfile" \
  --build-arg NODE_IMAGE="$node_image" \
  --build-arg NGINX_IMAGE="$nginx_image" \
  --tag "$frontend_image" "$repository_root/frontend"
docker run --detach --name "$frontend_container" --init \
  --network "$project"_default --read-only --user 101:101 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs /var/cache/nginx:rw,noexec,nosuid,nodev,size=32m,uid=101,gid=101 \
  --tmpfs /var/run:rw,noexec,nosuid,nodev,size=4m,uid=101,gid=101 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,uid=101,gid=101 \
  --publish "127.0.0.1:$WEB_PORT:8080" "$frontend_image" > /dev/null
frontend_url="http://127.0.0.1:$WEB_PORT"
for attempt in $(seq 1 30); do
  if curl --fail --silent "$frontend_url/healthz" > "$temporary_directory/frontend-health" 2>/dev/null; then
    break
  fi
  sleep 2
done
test "$(tr -d '\r\n' < "$temporary_directory/frontend-health")" = ok
test "$(docker inspect --format '{{.State.Running}}' "$frontend_container")" = true
curl --fail --silent --show-error "$frontend_url/" > "$temporary_directory/frontend-root.html"
curl --fail --silent --show-error "$frontend_url/history" > "$temporary_directory/frontend-history.html"
cmp "$temporary_directory/frontend-root.html" "$temporary_directory/frontend-history.html"
grep -q '<title>Vlytics</title>' "$temporary_directory/frontend-root.html"
curl --fail --silent --show-error --head "$frontend_url/" |
  tr -d '\r' | grep -qi '^content-security-policy:'
frontend_schedule_url="$frontend_url/api/v1/schedule?date=2026-09-25"
test "$(curl --silent --output /dev/null --write-out '%{http_code}' "$frontend_schedule_url")" = 401
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --header "Authorization: Bearer $VLYTICS_READONLY_AUTH_SECRET" "$frontend_schedule_url")" = 403
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --header "Authorization: Bearer $VLYTICS_OPERATOR_AUTH_SECRET" "$frontend_schedule_url")" = 200

migration_count="$(database_query 'SELECT count(*) FROM public.vlytics_schema_migrations;')"
test "$migration_count" -ge 1
test "$(database_query "SELECT rolsuper::text FROM pg_roles WHERE rolname = 'vlytics_migrator';")" = false
test "$(database_query "SELECT rolcanlogin::text FROM pg_roles WHERE rolname = 'vlytics_bootstrap_admin';")" = false

database_query "INSERT INTO ops.jobs (job_key, job_type, payload, due_at, deadline_at)
VALUES ('ci-disabled-source', 'mirror.pre_cutoff_sync', '{}'::jsonb,
        now() - interval '1 minute', now() + interval '10 minutes');" > /dev/null
job_query="SELECT state || ':' || attempt_no::text || ':' ||
  (SELECT count(*)::text FROM ops.job_attempts WHERE job_id = jobs.id)
  FROM ops.jobs AS jobs WHERE job_key = 'ci-disabled-source';"
job_state=""
for attempt in $(seq 1 30); do
  job_state="$(database_query "$job_query")"
  if test "$job_state" = "quarantined:1:1"; then
    break
  fi
  assert_running worker
  sleep 2
done
test "$job_state" = "quarantined:1:1"
test "$(database_query "SELECT error_code FROM ops.jobs WHERE job_key = 'ci-disabled-source';")" = source_collection_disabled_by_config

compose restart worker
sleep 5
assert_running worker
test "$(database_query "$job_query")" = "quarantined:1:1"

compose run --rm migrate
test "$(database_query 'SELECT count(*) FROM public.vlytics_schema_migrations;')" = "$migration_count"

compose exec -T postgres pg_dump -U vlytics_migrator -d vlytics --format=custom --no-owner --no-privileges > "$temporary_directory/backup.dump"
test -s "$temporary_directory/backup.dump"
docker run --detach --name "$restore_container" --network none \
  --env POSTGRES_PASSWORD=ci-restore-password \
  --env POSTGRES_DB=vlytics_restore postgres:17.11-alpine > /dev/null
for attempt in $(seq 1 30); do
  if docker exec "$restore_container" pg_isready -U postgres -d vlytics_restore > /dev/null 2>&1; then
    break
  fi
  sleep 2
done
docker cp "$temporary_directory/backup.dump" "$restore_container:/tmp/backup.dump"
docker exec "$restore_container" pg_restore -U postgres -d vlytics_restore \
  --no-owner --no-privileges --single-transaction --exit-on-error /tmp/backup.dump

restore_query() {
  docker exec "$restore_container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U postgres -d vlytics_restore -c "$1" | tr -d '\r'
}

ledger_query="SELECT string_agg(version || ':' || checksum, ',' ORDER BY version)
  FROM public.vlytics_schema_migrations;"
test "$(restore_query "$ledger_query")" = "$(database_query "$ledger_query")"
test "$(restore_query 'SELECT count(*) FROM ops.jobs;')" = 1
test "$(restore_query 'SELECT count(*) FROM ops.job_attempts;')" = 1
test "$(restore_query "$job_query")" = "quarantined:1:1"
test "$(restore_query "SELECT count(*) FROM pg_namespace WHERE nspname IN ('mirror', 'engine', 'market', 'ops');")" = 4

# Exercise the hardened production topology with the inactive example configuration.
production_compose config --quiet
production_compose up --detach postgres migrate api worker frontend
production_url="http://127.0.0.1:$VLYTICS_CI_PRODUCTION_WEB_PORT"
for attempt in $(seq 1 60); do
  if curl --fail --silent "$production_url/healthz" > "$temporary_directory/production-health" 2>/dev/null; then
    break
  fi
  sleep 2
done
test "$(tr -d '\r\n' < "$temporary_directory/production-health")" = ok
for service in postgres api worker frontend; do
  container_id="$(production_compose ps -q "$service")"
  test -n "$container_id"
  test "$(docker inspect --format '{{.State.Running}}' "$container_id")" = true
done
for service in api worker frontend; do
  container_id="$(production_compose ps -q "$service")"
  test "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$container_id")" = true
done
curl --fail --silent --show-error "$production_url/" > "$temporary_directory/production-root.html"
curl --fail --silent --show-error "$production_url/history" > "$temporary_directory/production-history.html"
cmp "$temporary_directory/production-root.html" "$temporary_directory/production-history.html"
curl --fail --silent --show-error --head "$production_url/" |
  tr -d '\r' | grep -qi '^content-security-policy:'
production_schedule_url="$production_url/api/v1/schedule?date=2026-09-25"
test "$(curl --silent --output /dev/null --write-out '%{http_code}' "$production_schedule_url")" = 401
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --header "Authorization: Bearer $VLYTICS_READONLY_AUTH_SECRET" "$production_schedule_url")" = 403
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --header "Authorization: Bearer $VLYTICS_OPERATOR_AUTH_SECRET" "$production_schedule_url")" = 200

echo "Compose smoke passed: development and inactive production stacks, API, frontend proxy, worker restart, migration replay, backup and isolated restore."
