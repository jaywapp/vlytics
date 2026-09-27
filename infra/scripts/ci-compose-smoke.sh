#!/usr/bin/env bash
set -Eeuo pipefail

repository_root="$(cd "$(dirname "$0")/../.." && pwd)"
project="vlytics-smoke-$(date +%s)-$RANDOM"
restore_container="$project-restore"
frontend_container="$project-frontend"
frontend_image="$project-frontend:ci"
temporary_directory="$(mktemp -d)"

compose() {
  docker compose --project-name "$project" --file "$repository_root/infra/compose.yaml" "$@"
}

cleanup() {
  result=$?
  trap - EXIT
  if (( result != 0 )); then
    compose ps || true
    compose logs --no-color --tail=120 postgres migrate api worker || true
    docker logs --tail=120 "$frontend_container" || true
  fi
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

for image in python:3.12.14-alpine3.24 ghcr.io/astral-sh/uv:0.12.5 postgres:17.11-alpine; do
  docker pull "$image" > /dev/null
done
export VLYTICS_PYTHON_BUILD_IMAGE="$(docker image inspect --format '{{index .RepoDigests 0}}' python:3.12.14-alpine3.24)"
export VLYTICS_UV_BUILD_IMAGE="$(docker image inspect --format '{{index .RepoDigests 0}}' ghcr.io/astral-sh/uv:0.12.5)"
export VLYTICS_POSTGRES_IMAGE="$(docker image inspect --format '{{index .RepoDigests 0}}' postgres:17.11-alpine)"
postgres_source_image="$VLYTICS_POSTGRES_IMAGE"
golang_source_image="golang:1.26.8-alpine3.24@sha256:8ac98ca534ac3f51e1f420a1dd2c15e74c75cfa0f23f3ad27eb5d7236c349a0c"
docker pull "$golang_source_image" > /dev/null
docker build --file "$repository_root/infra/images/postgres.Dockerfile" \
  --build-arg POSTGRES_IMAGE="$postgres_source_image" --tag "$project-postgres:ci" "$repository_root/infra/images"
export VLYTICS_POSTGRES_IMAGE="$project-postgres:ci"
python3 - "$repository_root" "$VLYTICS_POSTGRES_IMAGE" "$postgres_source_image" "$golang_source_image" <<'PY'
import json
import pathlib
import subprocess
import sys

root, postgres, upstream, compiler = sys.argv[1:]
files = ["gosu.sha256", "gosu-build-metadata.txt", "gosu-module-files.sha256",
         "gosu-module-verification.txt", "gosu-source-provenance.txt", "gosu-version.txt"]
evidence = {
    "schema_version": "gosu-build-provenance-v1",
    "postgres_upstream": upstream,
    "compiler": compiler,
    "image_id": subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", postgres], text=True).strip(),
    "files": {
        name: subprocess.check_output(["docker", "run", "--rm", "--entrypoint", "cat", postgres,
                                      "/usr/local/share/vlytics/" + name], text=True)
        for name in files
    },
}
path = pathlib.Path(root) / "artifacts/operations/gosu-build-provenance.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
PY

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
docker build --file "$repository_root/infra/images/node.Dockerfile" \
  --build-arg NODE_IMAGE="$node_image" --tag "$project-node:ci" "$repository_root/infra/images"
docker build --file "$repository_root/infra/images/nginx.Dockerfile" \
  --build-arg NGINX_IMAGE="$nginx_image" --tag "$project-nginx:ci" "$repository_root/infra/images"
node_source_image="$node_image"
nginx_source_image="$nginx_image"
node_image="$project-node:ci"
nginx_image="$project-nginx:ci"
docker build --file "$repository_root/frontend/Dockerfile" \
  --build-arg NODE_IMAGE="$node_image" \
  --build-arg NGINX_IMAGE="$nginx_image" \
  --tag "$frontend_image" "$repository_root/frontend"
python3 - "$repository_root" "$frontend_image" "$node_image" "$nginx_image" "$node_source_image" "$nginx_source_image" "$postgres_source_image" "$golang_source_image" <<'PY'
import json
import os
import pathlib
import subprocess
import sys

root = pathlib.Path(sys.argv[1])
path = root / "artifacts/operations/ci-image-inputs.json"
path.parent.mkdir(parents=True, exist_ok=True)
revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
path.write_text(json.dumps({
    "git_revision": revision,
    "build_inputs": {"node": sys.argv[5], "nginx": sys.argv[6], "postgres": sys.argv[7], "golang": sys.argv[8]},
    "images": {
        "backend": "vlytics-backend:local",
        "frontend": sys.argv[2],
        "python": os.environ["VLYTICS_PYTHON_BUILD_IMAGE"],
        "uv": os.environ["VLYTICS_UV_BUILD_IMAGE"],
        "postgres": os.environ["VLYTICS_POSTGRES_IMAGE"],
        "node": sys.argv[3],
        "nginx": sys.argv[4],
        "golang": sys.argv[8],
    },
}, indent=2) + "\n", encoding="utf-8")
PY

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
worker_restart_boundary="$(date --utc +%Y-%m-%dT%H:%M:%S.%NZ)"
sleep 5
assert_running worker
test "$(database_query "$job_query")" = "quarantined:1:1"

worker_container_id="$(compose ps -q worker)"
exported_heartbeat="$temporary_directory/worker-heartbeat.json"
heartbeat_export_verified=false
for attempt in $(seq 1 10); do
  if (
    cd "$repository_root/backend"
    uv run python -m vlytics.ops.heartbeat_export \
      --container "$worker_container_id" --output "$exported_heartbeat"
  ) && python3 - "$exported_heartbeat" "$worker_restart_boundary" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path

heartbeat = json.loads(Path(sys.argv[1]).read_text())
completed = datetime.fromisoformat(heartbeat["completed_at"])
restarted = datetime.fromisoformat(sys.argv[2])
raise SystemExit(0 if completed > restarted else 1)
PY
  then
    heartbeat_export_verified=true
    break
  fi
  sleep 1
done
test "$heartbeat_export_verified" = true
(
  cd "$repository_root/backend"
  uv run python -m vlytics.ops.heartbeat --path "$exported_heartbeat" --max-age-seconds 180
)
echo "Worker heartbeat export passed after restart."

compose run --rm migrate
test "$(database_query 'SELECT count(*) FROM public.vlytics_schema_migrations;')" = "$migration_count"

compose exec -T postgres pg_dump -U vlytics_migrator -d vlytics \
  --format=custom --create > "$temporary_directory/backup.dump"
test -s "$temporary_directory/backup.dump"
source_database_security="$(database_query "SELECT pg_get_userbyid(d.datdba) || '|' ||
  COALESCE(string_agg(
    (CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(acl.grantee) END) || ':' ||
    acl.privilege_type || ':' || acl.is_grantable::text,
    ',' ORDER BY CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(acl.grantee) END,
      acl.privilege_type, acl.is_grantable), '')
  FROM pg_database AS d
  CROSS JOIN LATERAL aclexplode(COALESCE(d.datacl, acldefault('d', d.datdba))) AS acl
  WHERE d.datname = current_database()
  GROUP BY d.datdba;")"

docker run --detach --name "$restore_container" --network none \
  --env POSTGRES_PASSWORD=ci-restore-password \
  --env POSTGRES_DB=vlytics_restore "$VLYTICS_POSTGRES_IMAGE" > /dev/null
for attempt in $(seq 1 30); do
  if docker exec "$restore_container" pg_isready -U postgres -d vlytics_restore > /dev/null 2>&1; then
    break
  fi
  sleep 2
done

restore_migrator_password="ci-restore-migrator-password"
restore_collector_password="ci-restore-collector-password"
restore_engine_password="ci-restore-engine-password"
restore_market_password="ci-restore-market-password"
restore_read_password="ci-restore-read-password"
docker exec -i \
  --env RESTORE_MIGRATOR_PASSWORD="$restore_migrator_password" \
  --env RESTORE_COLLECTOR_PASSWORD="$restore_collector_password" \
  --env RESTORE_ENGINE_PASSWORD="$restore_engine_password" \
  --env RESTORE_MARKET_PASSWORD="$restore_market_password" \
  --env RESTORE_READ_PASSWORD="$restore_read_password" \
  "$restore_container" psql -X -v ON_ERROR_STOP=1 -U postgres -d vlytics_restore <<'SQL'
CREATE ROLE vlytics_bootstrap_admin NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE vlytics_migration_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE vlytics_collector NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE vlytics_engine NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE vlytics_market_ingest NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE vlytics_read_api NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
\getenv migrator_password RESTORE_MIGRATOR_PASSWORD
\getenv collector_password RESTORE_COLLECTOR_PASSWORD
\getenv engine_password RESTORE_ENGINE_PASSWORD
\getenv market_password RESTORE_MARKET_PASSWORD
\getenv read_password RESTORE_READ_PASSWORD
SELECT format(
  'CREATE ROLE vlytics_migrator LOGIN NOSUPERUSER NOCREATEDB CREATEROLE INHERIT PASSWORD %L',
  :'migrator_password'
) \gexec
SELECT format(
  'CREATE ROLE vlytics_collector_login LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT PASSWORD %L',
  :'collector_password'
) \gexec
SELECT format(
  'CREATE ROLE vlytics_engine_login LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT PASSWORD %L',
  :'engine_password'
) \gexec
SELECT format(
  'CREATE ROLE vlytics_market_ingest_login LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT PASSWORD %L',
  :'market_password'
) \gexec
SELECT format(
  'CREATE ROLE vlytics_read_api_login LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT PASSWORD %L',
  :'read_password'
) \gexec
GRANT vlytics_migration_owner TO vlytics_migrator;
GRANT vlytics_collector TO vlytics_collector_login;
GRANT vlytics_engine TO vlytics_engine_login;
GRANT vlytics_market_ingest TO vlytics_market_ingest_login;
GRANT vlytics_read_api TO vlytics_read_api_login;
SQL

docker cp "$temporary_directory/backup.dump" "$restore_container:/tmp/backup.dump"
docker exec "$restore_container" pg_restore -U postgres -d postgres \
  --create --exit-on-error /tmp/backup.dump

restore_query() {
  docker exec "$restore_container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U postgres -d vlytics -c "$1" | tr -d '\r'
}

restore_login_query() {
  local role="$1"
  local password="$2"
  local statement="$3"
  docker exec --env PGPASSWORD="$password" "$restore_container" \
    psql -X -A -t -v ON_ERROR_STOP=1 -h 127.0.0.1 -U "$role" -d vlytics \
    -c "$statement" | tr -d '\r'
}

assert_login_denied() {
  local role="$1"
  local password="$2"
  local statement="$3"
  if restore_login_query "$role" "$password" "$statement" > /dev/null 2>&1; then
    echo "Expected PostgreSQL permission denial for $role" >&2
    return 1
  fi
}

ledger_query="SELECT string_agg(version || ':' || checksum, ',' ORDER BY version)
  FROM public.vlytics_schema_migrations;"
test "$(restore_query "$ledger_query")" = "$(database_query "$ledger_query")"
test "$(restore_query 'SELECT count(*) FROM ops.jobs;')" = 1
test "$(restore_query 'SELECT count(*) FROM ops.job_attempts;')" = 1
test "$(restore_query "$job_query")" = "quarantined:1:1"
test "$(restore_query "SELECT count(*) FROM pg_namespace WHERE nspname IN ('mirror', 'engine', 'market', 'ops');")" = 4
test "$(restore_query "SELECT count(*) FROM pg_namespace WHERE nspname IN ('mirror', 'engine', 'market', 'ops')
  AND pg_get_userbyid(nspowner) <> 'vlytics_migration_owner';")" = 0
test "$(restore_query "SELECT count(*) FROM pg_class AS c
  JOIN pg_namespace AS n ON n.oid = c.relnamespace
  WHERE n.nspname IN ('mirror', 'engine', 'market', 'ops')
  AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
  AND pg_get_userbyid(c.relowner) <> 'vlytics_migration_owner';")" = 0
restore_database_security="$(restore_query "SELECT pg_get_userbyid(d.datdba) || '|' ||
  COALESCE(string_agg(
    (CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(acl.grantee) END) || ':' ||
    acl.privilege_type || ':' || acl.is_grantable::text,
    ',' ORDER BY CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(acl.grantee) END,
      acl.privilege_type, acl.is_grantable), '')
  FROM pg_database AS d
  CROSS JOIN LATERAL aclexplode(COALESCE(d.datacl, acldefault('d', d.datdba))) AS acl
  WHERE d.datname = current_database()
  GROUP BY d.datdba;")"
test "$restore_database_security" = "$source_database_security"
test "$(restore_query "SELECT count(*) FROM pg_authid WHERE rolname IN (
  'vlytics_migrator', 'vlytics_collector_login', 'vlytics_engine_login',
  'vlytics_market_ingest_login', 'vlytics_read_api_login')
  AND rolpassword LIKE 'SCRAM-SHA-256$%';")" = 5

restore_login_query vlytics_migrator "$restore_migrator_password" \
  "BEGIN; SET LOCAL ROLE vlytics_migration_owner; CREATE SCHEMA ci_restore_probe; ROLLBACK;" > /dev/null
assert_login_denied vlytics_migrator "$restore_migrator_password" \
  "CREATE DATABASE ci_restore_forbidden;"
restore_login_query vlytics_collector_login "$restore_collector_password" \
  "INSERT INTO mirror.raw_snapshots SELECT * FROM mirror.raw_snapshots WHERE false;" > /dev/null
assert_login_denied vlytics_collector_login "$restore_collector_password" \
  "DELETE FROM mirror.raw_snapshots WHERE false;"
restore_login_query vlytics_engine_login "$restore_engine_password" \
  "INSERT INTO ops.jobs SELECT * FROM ops.jobs WHERE false;" > /dev/null
assert_login_denied vlytics_engine_login "$restore_engine_password" \
  "UPDATE engine.predictions SET id = id WHERE false;"
restore_login_query vlytics_market_ingest_login "$restore_market_password" \
  "INSERT INTO market.market_snapshots SELECT * FROM market.market_snapshots WHERE false;" > /dev/null
assert_login_denied vlytics_market_ingest_login "$restore_market_password" \
  "DELETE FROM market.market_snapshots WHERE false;"
restore_login_query vlytics_read_api_login "$restore_read_password" \
  "SELECT count(*) FROM engine.predictions;" > /dev/null
assert_login_denied vlytics_read_api_login "$restore_read_password" \
  "INSERT INTO engine.predictions SELECT * FROM engine.predictions WHERE false;"

live_run_id="compose-${project}"
live_manifest="$temporary_directory/live-e2e-manifest.json"
export VLYTICS_LIVE_E2E_DATABASE_URL="postgresql+psycopg://vlytics_migrator:${MIGRATOR_DATABASE_PASSWORD}@127.0.0.1:${POSTGRES_PORT}/vlytics"
uv run --project "$repository_root/backend" --locked python \
  "$repository_root/infra/scripts/seed_browser_smoke.py" \
  --run-id "$live_run_id" --output "$live_manifest" > /dev/null
test -s "$live_manifest"
(
  cd "$repository_root/frontend"
  VLYTICS_LIVE_E2E_BASE_URL="$frontend_url" \
  VLYTICS_LIVE_E2E_OPERATOR_TOKEN="$VLYTICS_OPERATOR_AUTH_SECRET" \
  VLYTICS_LIVE_E2E_SEED_MANIFEST="$live_manifest" \
    npm run test:e2e:live
)

echo "Compose smoke passed: API, frontend image and proxy, worker restart, migration replay, owner/ACL restore, SCRAM role boundaries, and live browser flow."
