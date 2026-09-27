#!/usr/bin/env bash
set -Eeuo pipefail

repository_root="$(cd "$(dirname "$0")/../.." && pwd)"
project="vlytics-production-internal-$(date +%s)-$RANDOM"
temporary_directory="$(mktemp -d)"
operational_config="$temporary_directory/operational.synthetic.toml"
provider_registry="$temporary_directory/providers.synthetic.toml"
dry_run_evidence="$temporary_directory/live-dry-run-evidence.synthetic.json"

: "${VLYTICS_POSTGRES_IMAGE:?VLYTICS_POSTGRES_IMAGE must name the existing CI PostgreSQL image}"
export VLYTICS_BACKEND_IMAGE="${VLYTICS_BACKEND_IMAGE:-vlytics-backend:local}"
# Compose interpolates every service even though this smoke never creates frontend.
export VLYTICS_FRONTEND_IMAGE="${VLYTICS_FRONTEND_IMAGE:-$VLYTICS_BACKEND_IMAGE}"

compose() {
  docker compose --project-name "$project" \
    --file "$repository_root/infra/compose.production.yaml" "$@"
}

cleanup() {
  result=$?
  trap - EXIT
  if (( result != 0 )); then
    compose ps || true
    compose logs --no-color --tail=120 postgres migrate api worker || true
  fi
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf -- "$temporary_directory"
  exit "$result"
}
trap cleanup EXIT

cat > "$operational_config" <<'TOML'
schema_version = "1.0"
environment = "production"
live_operations_enabled = true

[deployment]
live_enabled = true
host_class = "private_single_vm"
host_provider = "synthetic-ci-only"
host_region = "synthetic-ci-only"
monthly_cost_limit = 1
cost_currency = "USD"
network_access = "loopback"
operator_auth_method = "private_network_identity"
operator_secret_env = "VLYTICS_OPERATOR_AUTH_SECRET"
clock_sync_required = true

[database]
engine = "postgresql"
connection_url_env = "VLYTICS_DATABASE_URL"
raw_storage = "postgresql_private"

[backup]
enabled = true
strategy = "synthetic-ci-no-backup"
interval_hours = 24
retention_days = 1
rpo_minutes = 1440
rto_minutes = 1440
credential_env = "VLYTICS_BACKUP_CREDENTIAL"

[alerting]
enabled = true
channel = "generic_webhook_v1"
destination_env = "VLYTICS_ALERT_DESTINATION"

[ai]
live_calls_enabled = true
budget_currency = "USD"

[ai.openai]
enabled = true
model_id = "synthetic-openai-v1"
version_policy = "immutable_model_id"
pinned_model_version = "synthetic-openai-v1"
api_key_env = "VLYTICS_OPENAI_API_KEY"
daily_budget_amount = 1
monthly_budget_amount = 10
max_calls_per_match = 1
daily_call_limit = 1
monthly_call_limit = 1
max_input_tokens_per_call = 32
max_output_tokens_per_call = 16

[ai.anthropic]
enabled = true
model_id = "synthetic-anthropic-v1"
version_policy = "immutable_model_id"
pinned_model_version = "synthetic-anthropic-v1"
api_key_env = "VLYTICS_ANTHROPIC_API_KEY"
daily_budget_amount = 1
monthly_budget_amount = 10
max_calls_per_match = 1
daily_call_limit = 1
monthly_call_limit = 1
max_input_tokens_per_call = 32
max_output_tokens_per_call = 16

[ai.google]
enabled = true
model_id = "synthetic-google-v1"
version_policy = "immutable_model_id"
pinned_model_version = "synthetic-google-v1"
api_key_env = "VLYTICS_GOOGLE_API_KEY"
daily_budget_amount = 1
monthly_budget_amount = 10
max_calls_per_match = 1
daily_call_limit = 1
monthly_call_limit = 1
max_input_tokens_per_call = 32
max_output_tokens_per_call = 16

[timing]
target_cutoff_minutes = 60
initial_start_tolerance_seconds = 30
completion_grace_seconds = 300
request_timeout_seconds = 60
max_attempts = 3
retry_initial_delay_seconds = 5
retry_backoff_multiplier = 2
retry_max_delay_seconds = 30
allow_t10_retry = false
allow_completion_at_or_after_scheduled_start = false
allow_completion_at_or_after_actual_start = false
deadline_rule = "min(cutoff_at + completion_grace, scheduled_start_at, actual_start_at_if_known)"

[results]
poll_interval_seconds = 300
provisional_stability_seconds = 1800
correction_recheck_interval_seconds = 21600
correction_recheck_window_hours = 168

[source]
bulk_collection_enabled = false
permission_policy = "synthetic-ci-no-network"
max_requests_per_minute = 0
max_concurrency = 0
scopes = []

[activation]
dry_run_evidence_max_age_hours = 24
dry_run_evidence_max_bytes = 16384
TOML

cat > "$provider_registry" <<'TOML'
schema_version = "1"

[budget]
resolved = true
daily_amount = "3"
monthly_amount = "30"

[[variants]]
id = "synthetic-openai-v1"
provider = "openai"
enabled = true
op003_resolved = true
requested_model_id = "synthetic-openai-v1"
pinned_model_version = "synthetic-openai-v1"
version_policy = "immutable_model_id"
prompt_version = "independent-volleyball-v1"
prompt_hash = "167c7845bf81d4da3dde37e5f53620d99428fa725b72713c578bb1ec41e219db"
distribution_version = "ai-direct-set-v1"
max_input_tokens = 32
max_output_tokens = 16
input_cost_per_million = "1"
output_cost_per_million = "1"

[[variants]]
id = "synthetic-anthropic-v1"
provider = "anthropic"
enabled = true
op003_resolved = true
requested_model_id = "synthetic-anthropic-v1"
pinned_model_version = "synthetic-anthropic-v1"
version_policy = "immutable_model_id"
prompt_version = "independent-volleyball-v1"
prompt_hash = "167c7845bf81d4da3dde37e5f53620d99428fa725b72713c578bb1ec41e219db"
distribution_version = "ai-direct-set-v1"
max_input_tokens = 32
max_output_tokens = 16
input_cost_per_million = "1"
output_cost_per_million = "1"

[[variants]]
id = "synthetic-google-v1"
provider = "google"
enabled = true
op003_resolved = true
requested_model_id = "synthetic-google-v1"
pinned_model_version = "synthetic-google-v1"
version_policy = "immutable_model_id"
prompt_version = "independent-volleyball-v1"
prompt_hash = "167c7845bf81d4da3dde37e5f53620d99428fa725b72713c578bb1ec41e219db"
distribution_version = "ai-direct-set-v1"
max_input_tokens = 32
max_output_tokens = 16
input_cost_per_million = "1"
output_cost_per_million = "1"
TOML

python3 - "$operational_config" "$dry_run_evidence" <<'PY'
import hashlib
import json
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

config_path, evidence_path = map(Path, sys.argv[1:])
with config_path.open("rb") as stream:
    config = tomllib.load(stream)
canonical = json.dumps(
    config,
    ensure_ascii=False,
    allow_nan=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
evidence = {
    "schema_version": "1.0",
    "config_sha256": hashlib.sha256(canonical).hexdigest(),
    "completed_at": datetime.now(UTC).isoformat(),
    "source_sync_verified": True,
    "freeze_verified": True,
    "providers_verified": ["openai", "anthropic", "google"],
}
Path(evidence_path).write_text(
    json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY
# Individual file bind mounts remain readable by the non-root backend UID under restrictive umask.
chmod 0644 "$operational_config" "$provider_registry" "$dry_run_evidence"

export VLYTICS_OPERATIONAL_CONFIG_FILE="$operational_config"
export VLYTICS_PROVIDER_REGISTRY_FILE="$provider_registry"
export VLYTICS_SOURCE_REGISTRY_FILE="$repository_root/config/source.toml"
export VLYTICS_LIVE_DRY_RUN_EVIDENCE_FILE="$dry_run_evidence"
export POSTGRES_DB="vlytics"
export MIGRATOR_DATABASE_PASSWORD="synthetic-ci-migrator"
export COLLECTOR_DATABASE_PASSWORD="synthetic-ci-collector"
export ENGINE_DATABASE_PASSWORD="synthetic-ci-engine"
export MARKET_INGEST_DATABASE_PASSWORD="synthetic-ci-market"
export READ_API_DATABASE_PASSWORD="synthetic-ci-read-api"
export MIGRATOR_DATABASE_URL="postgresql+psycopg://vlytics_migrator:synthetic-ci-migrator@postgres:5432/vlytics"
export COLLECTOR_DATABASE_URL="postgresql+psycopg://vlytics_collector_login:synthetic-ci-collector@postgres:5432/vlytics"
export ENGINE_DATABASE_URL="postgresql+psycopg://vlytics_engine_login:synthetic-ci-engine@postgres:5432/vlytics"
export READ_API_DATABASE_URL="postgresql+psycopg://vlytics_read_api_login:synthetic-ci-read-api@postgres:5432/vlytics"
export VLYTICS_OPERATOR_AUTH_SECRET="synthetic-ci-operator"
export VLYTICS_READONLY_AUTH_SECRET="synthetic-ci-readonly"
export VLYTICS_OPENAI_API_KEY="synthetic-ci-openai-never-sent"
export VLYTICS_ANTHROPIC_API_KEY="synthetic-ci-anthropic-never-sent"
export VLYTICS_GOOGLE_API_KEY="synthetic-ci-google-never-sent"

# These fixtures prove role-specific startup only. They are not live-operation approval evidence.
docker image inspect "$VLYTICS_BACKEND_IMAGE" "$VLYTICS_POSTGRES_IMAGE" >/dev/null
compose config --quiet
compose up --detach --no-build --pull never postgres migrate api worker

test -z "$(compose ps -q frontend)"
network_id="$(docker network ls --quiet \
  --filter "label=com.docker.compose.project=$project" \
  --filter "label=com.docker.compose.network=private")"
test -n "$network_id"
test "$(docker network inspect --format '{{.Internal}}' "$network_id")" = true

migrate_id="$(compose ps --all --quiet migrate)"
test -n "$migrate_id"
test "$(docker inspect --format '{{.State.ExitCode}}' "$migrate_id")" = 0

api_ready=false
for attempt in $(seq 1 60); do
  if compose exec -T api python -c \
    "import json,urllib.request; value=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)); assert value == {'status':'ok','service':'vlytics-api'}"; then
    api_ready=true
    break
  fi
  sleep 2
done
test "$api_ready" = true

compose exec -T api python - <<'PY'
import urllib.error
import urllib.request

url = "http://127.0.0.1:8000/api/v1/schedule?date=2026-09-27"
try:
    urllib.request.urlopen(url, timeout=3)
except urllib.error.HTTPError as error:
    assert error.code == 401
else:
    raise AssertionError("anonymous API request was not rejected")
request = urllib.request.Request(
    url,
    headers={"Authorization": "Bearer synthetic-ci-operator"},
)
with urllib.request.urlopen(request, timeout=3) as response:
    assert response.status == 200
PY

heartbeat_time() {
  compose exec -T worker python -c \
    "import json,pathlib; print(json.loads(pathlib.Path('/tmp/vlytics-worker-heartbeat.json').read_text())['completed_at'])"
}

first_heartbeat=""
for attempt in $(seq 1 60); do
  if first_heartbeat="$(heartbeat_time 2>/dev/null)" && test -n "$first_heartbeat"; then
    break
  fi
  sleep 2
done
test -n "$first_heartbeat"

second_heartbeat="$first_heartbeat"
for attempt in $(seq 1 15); do
  sleep 1
  second_heartbeat="$(heartbeat_time 2>/dev/null || true)"
  if test -n "$second_heartbeat" && python3 - "$first_heartbeat" "$second_heartbeat" <<'PY'
import sys
from datetime import datetime

first = datetime.fromisoformat(sys.argv[1])
second = datetime.fromisoformat(sys.argv[2])
raise SystemExit(0 if second > first else 1)
PY
  then
    break
  fi
done
python3 - "$first_heartbeat" "$second_heartbeat" <<'PY'
import sys
from datetime import datetime

assert datetime.fromisoformat(sys.argv[2]) > datetime.fromisoformat(sys.argv[1])
PY

counts="$(compose exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
  -U vlytics_migrator -d vlytics -c \
  "SELECT (SELECT count(*) FROM mirror.raw_snapshots) || ':' ||
          (SELECT count(*) FROM engine.prediction_attempts) || ':' ||
          (SELECT count(*) FROM ops.provider_budget_reservations) || ':' ||
          (SELECT count(*) FROM ops.jobs);")"
test "$(printf '%s' "$counts" | tr -d '\r\n')" = "0:0:0:0"

echo "Synthetic production internal smoke passed: API and worker started with role-specific secrets; no source or provider work was created."