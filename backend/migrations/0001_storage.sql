DO $roles$
BEGIN
    IF current_user <> 'vlytics_migrator' THEN
        RAISE EXCEPTION 'migrations must run as vlytics_migrator, got %', current_user;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vlytics_migration_owner') THEN
        CREATE ROLE vlytics_migration_owner
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vlytics_collector') THEN
        CREATE ROLE vlytics_collector
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vlytics_engine') THEN
        CREATE ROLE vlytics_engine
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vlytics_market_ingest') THEN
        CREATE ROLE vlytics_market_ingest
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vlytics_read_api') THEN
        CREATE ROLE vlytics_read_api
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vlytics_collector_login') THEN
        CREATE ROLE vlytics_collector_login LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vlytics_engine_login') THEN
        CREATE ROLE vlytics_engine_login LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vlytics_market_ingest_login') THEN
        CREATE ROLE vlytics_market_ingest_login LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vlytics_read_api_login') THEN
        CREATE ROLE vlytics_read_api_login LOGIN;
    END IF;
END
$roles$;

ALTER ROLE vlytics_migration_owner
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION;
ALTER ROLE vlytics_collector
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION;
ALTER ROLE vlytics_engine
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION;
ALTER ROLE vlytics_market_ingest
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION;
ALTER ROLE vlytics_read_api
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION;
ALTER ROLE vlytics_collector_login
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION;
ALTER ROLE vlytics_engine_login
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION;
ALTER ROLE vlytics_market_ingest_login
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION;
ALTER ROLE vlytics_read_api_login
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION;

GRANT vlytics_migration_owner TO vlytics_migrator;
GRANT vlytics_collector TO vlytics_collector_login;
GRANT vlytics_engine TO vlytics_engine_login;
GRANT vlytics_market_ingest TO vlytics_market_ingest_login;
GRANT vlytics_read_api TO vlytics_read_api_login;

DO $database_privileges$
BEGIN
    EXECUTE format(
        'GRANT CREATE ON DATABASE %I TO vlytics_migration_owner',
        current_database()
    );
END
$database_privileges$;

SET LOCAL ROLE vlytics_migration_owner;

CREATE SCHEMA mirror AUTHORIZATION vlytics_migration_owner;
CREATE SCHEMA engine AUTHORIZATION vlytics_migration_owner;
CREATE SCHEMA market AUTHORIZATION vlytics_migration_owner;
CREATE SCHEMA ops AUTHORIZATION vlytics_migration_owner;

CREATE TABLE mirror.raw_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source text NOT NULL CHECK (btrim(source) <> ''),
    source_group_code text NOT NULL CHECK (btrim(source_group_code) <> ''),
    request_fingerprint text NOT NULL CHECK (btrim(request_fingerprint) <> ''),
    redacted_url text NOT NULL CHECK (btrim(redacted_url) <> ''),
    requested_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    status_code integer CHECK (status_code BETWEEN 100 AND 599),
    retry_after_seconds integer CHECK (retry_after_seconds >= 0),
    body_bytes bytea,
    private_uri text,
    sha256 text CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    parser_version text NOT NULL CHECK (btrim(parser_version) <> ''),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CHECK (received_at >= requested_at),
    CHECK (num_nonnulls(body_bytes, private_uri) <= 1),
    CHECK (private_uri IS NULL OR btrim(private_uri) <> ''),
    CHECK ((sha256 IS NOT NULL) = (num_nonnulls(body_bytes, private_uri) = 1)),
    CHECK (body_bytes IS NULL OR sha256 = encode(sha256(body_bytes), 'hex'))
);

CREATE TABLE mirror.seasons (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source text NOT NULL CHECK (btrim(source) <> ''),
    source_group_code text NOT NULL CHECK (btrim(source_group_code) <> ''),
    source_season_code text NOT NULL CHECK (btrim(source_season_code) <> ''),
    label text NOT NULL CHECK (btrim(label) <> ''),
    starts_at timestamptz,
    ends_at timestamptz,
    observed_at timestamptz NOT NULL,
    raw_snapshot_id uuid NOT NULL REFERENCES mirror.raw_snapshots(id),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (source, source_group_code, source_season_code),
    CHECK (ends_at IS NULL OR starts_at IS NULL OR ends_at >= starts_at)
);

CREATE TABLE mirror.competitions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source text NOT NULL CHECK (btrim(source) <> ''),
    source_group_code text NOT NULL CHECK (btrim(source_group_code) <> ''),
    season_id uuid NOT NULL REFERENCES mirror.seasons(id),
    source_competition_code text NOT NULL CHECK (btrim(source_competition_code) <> ''),
    division text NOT NULL CHECK (division IN ('men', 'women')),
    stage text NOT NULL CHECK (stage IN ('regular', 'playoff', 'championship', 'other')),
    label text NOT NULL CHECK (btrim(label) <> ''),
    mapping_version text NOT NULL CHECK (btrim(mapping_version) <> ''),
    observed_at timestamptz NOT NULL,
    raw_snapshot_id uuid NOT NULL REFERENCES mirror.raw_snapshots(id),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (season_id, source_competition_code, division),
    UNIQUE (id, season_id)
);

CREATE TABLE mirror.franchises (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name text NOT NULL CHECK (btrim(canonical_name) <> ''),
    mapping_version text NOT NULL CHECK (btrim(mapping_version) <> ''),
    evidence text NOT NULL CHECK (btrim(evidence) <> ''),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp()
);

CREATE TABLE mirror.team_identities (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source text NOT NULL CHECK (btrim(source) <> ''),
    source_team_code text NOT NULL CHECK (btrim(source_team_code) <> ''),
    season_id uuid NOT NULL REFERENCES mirror.seasons(id),
    franchise_id uuid REFERENCES mirror.franchises(id),
    display_name text NOT NULL CHECK (btrim(display_name) <> ''),
    valid_from timestamptz,
    valid_to timestamptz,
    observed_at timestamptz NOT NULL,
    mapping_version text NOT NULL CHECK (btrim(mapping_version) <> ''),
    raw_snapshot_id uuid NOT NULL REFERENCES mirror.raw_snapshots(id),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (season_id, source_team_code),
    CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
);

CREATE TABLE mirror.players (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source text NOT NULL CHECK (btrim(source) <> ''),
    season_id uuid NOT NULL REFERENCES mirror.seasons(id),
    source_player_code text NOT NULL CHECK (btrim(source_player_code) <> ''),
    display_name text,
    first_observed_at timestamptz NOT NULL,
    raw_snapshot_id uuid NOT NULL REFERENCES mirror.raw_snapshots(id),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (season_id, source_player_code),
    CHECK (display_name IS NULL OR btrim(display_name) <> '')
);

CREATE TABLE mirror.venues (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source text NOT NULL CHECK (btrim(source) <> ''),
    source_venue_code text NOT NULL CHECK (btrim(source_venue_code) <> ''),
    name text NOT NULL CHECK (btrim(name) <> ''),
    mapping_version text NOT NULL CHECK (btrim(mapping_version) <> ''),
    observed_at timestamptz NOT NULL,
    raw_snapshot_id uuid NOT NULL REFERENCES mirror.raw_snapshots(id),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (source, source_venue_code)
);

CREATE TABLE mirror.matches (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source text NOT NULL CHECK (btrim(source) <> ''),
    source_group_code text NOT NULL CHECK (btrim(source_group_code) <> ''),
    source_season_code text NOT NULL CHECK (btrim(source_season_code) <> ''),
    source_competition_code text NOT NULL CHECK (btrim(source_competition_code) <> ''),
    source_match_code text NOT NULL CHECK (btrim(source_match_code) <> ''),
    season_id uuid NOT NULL REFERENCES mirror.seasons(id),
    competition_id uuid NOT NULL REFERENCES mirror.competitions(id),
    first_observed_at timestamptz NOT NULL,
    raw_snapshot_id uuid NOT NULL REFERENCES mirror.raw_snapshots(id),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (
        source,
        source_group_code,
        source_season_code,
        source_competition_code,
        source_match_code
    ),
    FOREIGN KEY (competition_id, season_id)
        REFERENCES mirror.competitions(id, season_id)
);

CREATE TABLE mirror.match_revisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    match_id uuid NOT NULL REFERENCES mirror.matches(id),
    revision integer NOT NULL CHECK (revision > 0),
    home_team_id uuid NOT NULL REFERENCES mirror.team_identities(id),
    away_team_id uuid NOT NULL REFERENCES mirror.team_identities(id),
    venue_id uuid REFERENCES mirror.venues(id),
    scheduled_start_at timestamptz NOT NULL,
    actual_start_at timestamptz,
    status text NOT NULL CHECK (btrim(status) <> ''),
    observed_at timestamptz NOT NULL,
    raw_snapshot_id uuid NOT NULL REFERENCES mirror.raw_snapshots(id),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (match_id, revision),
    UNIQUE (id, match_id),
    CHECK (home_team_id <> away_team_id)
);

CREATE TABLE mirror.roster_revisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    season_id uuid NOT NULL REFERENCES mirror.seasons(id),
    team_identity_id uuid NOT NULL REFERENCES mirror.team_identities(id),
    player_id uuid NOT NULL REFERENCES mirror.players(id),
    source_row_key text NOT NULL CHECK (btrim(source_row_key) <> ''),
    valid_from timestamptz,
    valid_to timestamptz,
    observed_at timestamptz NOT NULL,
    raw_snapshot_id uuid NOT NULL REFERENCES mirror.raw_snapshots(id),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (season_id, team_identity_id, player_id, observed_at, source_row_key),
    CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
);

CREATE TABLE mirror.result_revisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    match_id uuid NOT NULL REFERENCES mirror.matches(id),
    revision integer NOT NULL CHECK (revision > 0),
    home_sets smallint NOT NULL CHECK (home_sets BETWEEN 0 AND 5),
    away_sets smallint NOT NULL CHECK (away_sets BETWEEN 0 AND 5),
    home_points integer NOT NULL CHECK (home_points >= 0),
    away_points integer NOT NULL CHECK (away_points >= 0),
    finality text NOT NULL CHECK (finality IN ('provisional', 'final', 'corrected', 'void')),
    observed_at timestamptz NOT NULL,
    raw_snapshot_id uuid NOT NULL REFERENCES mirror.raw_snapshots(id),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (match_id, revision),
    UNIQUE (id, match_id)
);

CREATE TABLE mirror.match_sets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    result_revision_id uuid NOT NULL REFERENCES mirror.result_revisions(id),
    set_number smallint NOT NULL CHECK (set_number BETWEEN 1 AND 5),
    home_points integer NOT NULL CHECK (home_points >= 0),
    away_points integer NOT NULL CHECK (away_points >= 0),
    duration_seconds integer CHECK (duration_seconds > 0),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (result_revision_id, set_number)
);

CREATE TABLE mirror.team_match_stats (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    result_revision_id uuid NOT NULL REFERENCES mirror.result_revisions(id),
    team_identity_id uuid NOT NULL REFERENCES mirror.team_identities(id),
    source_row_key text NOT NULL CHECK (btrim(source_row_key) <> ''),
    metric_schema_version text NOT NULL CHECK (btrim(metric_schema_version) <> ''),
    metrics_json jsonb NOT NULL CHECK (jsonb_typeof(metrics_json) = 'object'),
    observed_at timestamptz NOT NULL,
    raw_snapshot_id uuid NOT NULL REFERENCES mirror.raw_snapshots(id),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (result_revision_id, team_identity_id)
);

CREATE TABLE mirror.player_match_stats (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    result_revision_id uuid NOT NULL REFERENCES mirror.result_revisions(id),
    player_id uuid NOT NULL REFERENCES mirror.players(id),
    team_identity_id uuid NOT NULL REFERENCES mirror.team_identities(id),
    source_row_key text NOT NULL CHECK (btrim(source_row_key) <> ''),
    metric_schema_version text NOT NULL CHECK (btrim(metric_schema_version) <> ''),
    metrics_json jsonb NOT NULL CHECK (jsonb_typeof(metrics_json) = 'object'),
    observed_at timestamptz NOT NULL,
    raw_snapshot_id uuid NOT NULL REFERENCES mirror.raw_snapshots(id),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (result_revision_id, player_id)
);

CREATE TABLE mirror.source_coverage (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source text NOT NULL CHECK (btrim(source) <> ''),
    season_id uuid REFERENCES mirror.seasons(id),
    competition_id uuid REFERENCES mirror.competitions(id),
    match_id uuid REFERENCES mirror.matches(id),
    data_kind text NOT NULL CHECK (btrim(data_kind) <> ''),
    availability text NOT NULL
        CHECK (availability IN ('available', 'missing', 'not_supported', 'unverified')),
    evidence text NOT NULL CHECK (btrim(evidence) <> ''),
    observed_at timestamptz NOT NULL,
    raw_snapshot_id uuid REFERENCES mirror.raw_snapshots(id),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp()
);

CREATE TABLE ops.jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_key text NOT NULL UNIQUE CHECK (btrim(job_key) <> ''),
    job_type text NOT NULL CHECK (btrim(job_type) <> ''),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
    due_at timestamptz NOT NULL,
    deadline_at timestamptz,
    state text NOT NULL DEFAULT 'queued'
        CHECK (state IN (
            'queued',
            'running',
            'retry_wait',
            'succeeded',
            'failed',
            'cancelled',
            'expired',
            'quarantined'
        )),
    lease_owner text,
    lease_until timestamptz,
    attempt_no integer NOT NULL DEFAULT 0 CHECK (attempt_no >= 0),
    error_code text,
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CHECK (deadline_at IS NULL OR deadline_at >= due_at),
    CHECK ((lease_owner IS NULL) = (lease_until IS NULL)),
    CHECK ((state = 'running') = (lease_owner IS NOT NULL)),
    CHECK (lease_owner IS NULL OR btrim(lease_owner) <> ''),
    CHECK (error_code IS NULL OR btrim(error_code) <> '')
);

CREATE TABLE engine.feature_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    match_id uuid NOT NULL REFERENCES mirror.matches(id),
    schedule_revision_id uuid NOT NULL REFERENCES mirror.match_revisions(id),
    cutoff_at timestamptz NOT NULL,
    captured_at timestamptz NOT NULL,
    feature_version text NOT NULL CHECK (btrim(feature_version) <> ''),
    availability_policy text NOT NULL
        CHECK (availability_policy IN (
            'historical_reconstruction',
            'historical_point_in_time',
            'live_prospective'
        )),
    lineup_status text NOT NULL CHECK (lineup_status IN ('known', 'partial', 'unknown')),
    values_json jsonb NOT NULL CHECK (jsonb_typeof(values_json) = 'object'),
    lineage_json jsonb NOT NULL CHECK (jsonb_typeof(lineage_json) = 'object'),
    sha256 text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (match_id, schedule_revision_id, feature_version, cutoff_at),
    UNIQUE (id, match_id, schedule_revision_id),
    FOREIGN KEY (schedule_revision_id, match_id)
        REFERENCES mirror.match_revisions(id, match_id)
);

CREATE TABLE engine.model_variants (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider text NOT NULL CHECK (btrim(provider) <> ''),
    requested_model text NOT NULL CHECK (btrim(requested_model) <> ''),
    pinned_model_version text,
    prompt_version text NOT NULL CHECK (btrim(prompt_version) <> ''),
    prompt_hash text NOT NULL CHECK (prompt_hash ~ '^[a-f0-9]{64}$'),
    feature_version text NOT NULL CHECK (btrim(feature_version) <> ''),
    output_schema_version text NOT NULL CHECK (btrim(output_schema_version) <> ''),
    hyperparameters jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(hyperparameters) = 'object'),
    experiment_group text,
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE NULLS NOT DISTINCT (
        provider,
        requested_model,
        pinned_model_version,
        prompt_hash,
        feature_version,
        output_schema_version,
        hyperparameters,
        experiment_group
    ),
    CHECK (pinned_model_version IS NULL OR btrim(pinned_model_version) <> ''),
    CHECK (experiment_group IS NULL OR btrim(experiment_group) <> '')
);

CREATE TABLE engine.prediction_attempts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL REFERENCES ops.jobs(id),
    snapshot_id uuid NOT NULL REFERENCES engine.feature_snapshots(id),
    variant_id uuid NOT NULL REFERENCES engine.model_variants(id),
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    status text NOT NULL
        CHECK (status IN ('succeeded', 'failed', 'timed_out', 'budget_skipped', 'late_rejected')),
    error_code text,
    request_hash text NOT NULL CHECK (request_hash ~ '^[a-f0-9]{64}$'),
    response_hash text CHECK (response_hash ~ '^[a-f0-9]{64}$'),
    response_private_uri text,
    usage_json jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(usage_json) = 'object'),
    latency_ms integer CHECK (latency_ms >= 0),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CHECK (completed_at IS NULL OR completed_at >= started_at),
    CHECK (response_private_uri IS NULL OR btrim(response_private_uri) <> '')
);

CREATE TABLE market.market_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    match_id uuid NOT NULL REFERENCES mirror.matches(id),
    source text NOT NULL CHECK (btrim(source) <> ''),
    source_event_id text NOT NULL CHECK (btrim(source_event_id) <> ''),
    quoted_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    contract_version text NOT NULL CHECK (btrim(contract_version) <> ''),
    markets_json jsonb NOT NULL CHECK (jsonb_typeof(markets_json) = 'object'),
    sha256 text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (source, source_event_id, quoted_at, contract_version, sha256),
    UNIQUE (id, match_id),
    CHECK (received_at >= observed_at)
);

CREATE TABLE engine.predictions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    match_id uuid NOT NULL REFERENCES mirror.matches(id),
    schedule_revision_id uuid NOT NULL REFERENCES mirror.match_revisions(id),
    snapshot_id uuid NOT NULL REFERENCES engine.feature_snapshots(id),
    variant_id uuid NOT NULL REFERENCES engine.model_variants(id),
    stage text NOT NULL CHECK (btrim(stage) <> ''),
    input_cutoff_at timestamptz NOT NULL,
    started_at timestamptz NOT NULL,
    generated_at timestamptz NOT NULL,
    resolved_model_id text NOT NULL CHECK (btrim(resolved_model_id) <> ''),
    output_json jsonb NOT NULL CHECK (jsonb_typeof(output_json) = 'object'),
    market_snapshot_id uuid REFERENCES market.market_snapshots(id),
    sha256 text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (snapshot_id, variant_id, stage),
    UNIQUE (id, match_id),
    FOREIGN KEY (snapshot_id, match_id, schedule_revision_id)
        REFERENCES engine.feature_snapshots(id, match_id, schedule_revision_id),
    FOREIGN KEY (market_snapshot_id, match_id)
        REFERENCES market.market_snapshots(id, match_id),
    CHECK (generated_at >= started_at)
);

CREATE TABLE engine.prediction_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    prediction_id uuid NOT NULL REFERENCES engine.predictions(id),
    event_type text NOT NULL
        CHECK (event_type IN ('published', 'voided', 'superseded', 'late_rejected')),
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    occurred_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    superseding_prediction_id uuid REFERENCES engine.predictions(id),
    schedule_revision_id uuid REFERENCES mirror.match_revisions(id),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (id, prediction_id),
    CHECK (prediction_id <> superseding_prediction_id),
    CHECK ((event_type = 'superseded') = (superseding_prediction_id IS NOT NULL))
);

CREATE TABLE engine.prediction_status_projection (
    prediction_id uuid PRIMARY KEY REFERENCES engine.predictions(id) ON DELETE CASCADE,
    latest_event_id uuid NOT NULL REFERENCES engine.prediction_events(id),
    current_status text NOT NULL
        CHECK (current_status IN ('published', 'voided', 'superseded', 'late_rejected')),
    refreshed_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    FOREIGN KEY (latest_event_id, prediction_id)
        REFERENCES engine.prediction_events(id, prediction_id)
);

CREATE TABLE market.market_evaluations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    match_id uuid NOT NULL REFERENCES mirror.matches(id),
    prediction_id uuid NOT NULL REFERENCES engine.predictions(id),
    market_snapshot_id uuid NOT NULL REFERENCES market.market_snapshots(id),
    evaluator_version text NOT NULL CHECK (btrim(evaluator_version) <> ''),
    derived_probabilities jsonb NOT NULL
        CHECK (jsonb_typeof(derived_probabilities) = 'object'),
    eligibility text NOT NULL CHECK (eligibility IN ('eligible', 'missing', 'ineligible')),
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (prediction_id, market_snapshot_id, evaluator_version),
    FOREIGN KEY (prediction_id, match_id)
        REFERENCES engine.predictions(id, match_id),
    FOREIGN KEY (market_snapshot_id, match_id)
        REFERENCES market.market_snapshots(id, match_id)
);

CREATE TABLE engine.evaluations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    match_id uuid NOT NULL REFERENCES mirror.matches(id),
    prediction_id uuid NOT NULL REFERENCES engine.predictions(id),
    result_revision_id uuid NOT NULL REFERENCES mirror.result_revisions(id),
    evaluator_version text NOT NULL CHECK (btrim(evaluator_version) <> ''),
    cohort_policy_version text NOT NULL CHECK (btrim(cohort_policy_version) <> ''),
    metric_values jsonb NOT NULL CHECK (jsonb_typeof(metric_values) = 'object'),
    settlement jsonb NOT NULL CHECK (jsonb_typeof(settlement) = 'object'),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (
        prediction_id,
        result_revision_id,
        evaluator_version,
        cohort_policy_version
    ),
    FOREIGN KEY (prediction_id, match_id)
        REFERENCES engine.predictions(id, match_id),
    FOREIGN KEY (result_revision_id, match_id)
        REFERENCES mirror.result_revisions(id, match_id)
);

CREATE TABLE ops.job_attempts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL REFERENCES ops.jobs(id),
    attempt_no integer NOT NULL CHECK (attempt_no > 0),
    lease_owner text NOT NULL CHECK (btrim(lease_owner) <> ''),
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    outcome text CHECK (outcome IN ('succeeded', 'failed', 'timed_out', 'abandoned')),
    error_code text,
    details jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(details) = 'object'),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (job_id, attempt_no),
    CHECK (completed_at IS NULL OR completed_at >= started_at),
    CHECK (error_code IS NULL OR btrim(error_code) <> '')
);

CREATE TABLE ops.audit_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor text NOT NULL CHECK (btrim(actor) <> ''),
    action text NOT NULL CHECK (btrim(action) <> ''),
    object_type text NOT NULL CHECK (btrim(object_type) <> ''),
    object_id text NOT NULL CHECK (btrim(object_id) <> ''),
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    correlation_id uuid NOT NULL,
    occurred_at timestamptz NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp()
);

CREATE INDEX raw_snapshots_request_received_idx
    ON mirror.raw_snapshots (source, source_group_code, request_fingerprint, received_at DESC);
CREATE INDEX seasons_raw_snapshot_idx ON mirror.seasons (raw_snapshot_id);
CREATE INDEX competitions_raw_snapshot_idx ON mirror.competitions (raw_snapshot_id);
CREATE INDEX team_identities_franchise_idx ON mirror.team_identities (franchise_id);
CREATE INDEX team_identities_raw_snapshot_idx ON mirror.team_identities (raw_snapshot_id);
CREATE INDEX players_raw_snapshot_idx ON mirror.players (raw_snapshot_id);
CREATE INDEX venues_raw_snapshot_idx ON mirror.venues (raw_snapshot_id);
CREATE INDEX matches_season_idx ON mirror.matches (season_id);
CREATE INDEX matches_competition_idx ON mirror.matches (competition_id);
CREATE INDEX matches_raw_snapshot_idx ON mirror.matches (raw_snapshot_id);
CREATE INDEX match_revisions_schedule_idx
    ON mirror.match_revisions (scheduled_start_at, match_id, revision DESC);
CREATE INDEX match_revisions_home_team_idx ON mirror.match_revisions (home_team_id);
CREATE INDEX match_revisions_away_team_idx ON mirror.match_revisions (away_team_id);
CREATE INDEX match_revisions_venue_idx ON mirror.match_revisions (venue_id);
CREATE INDEX match_revisions_raw_snapshot_idx ON mirror.match_revisions (raw_snapshot_id);
CREATE INDEX roster_revisions_team_observed_idx
    ON mirror.roster_revisions (team_identity_id, observed_at DESC);
CREATE INDEX roster_revisions_player_idx ON mirror.roster_revisions (player_id);
CREATE INDEX roster_revisions_raw_snapshot_idx ON mirror.roster_revisions (raw_snapshot_id);
CREATE INDEX result_revisions_match_observed_idx
    ON mirror.result_revisions (match_id, observed_at DESC);
CREATE INDEX result_revisions_raw_snapshot_idx ON mirror.result_revisions (raw_snapshot_id);
CREATE INDEX team_match_stats_team_idx ON mirror.team_match_stats (team_identity_id);
CREATE INDEX team_match_stats_raw_snapshot_idx ON mirror.team_match_stats (raw_snapshot_id);
CREATE INDEX player_match_stats_player_idx ON mirror.player_match_stats (player_id);
CREATE INDEX player_match_stats_team_idx ON mirror.player_match_stats (team_identity_id);
CREATE INDEX player_match_stats_raw_snapshot_idx ON mirror.player_match_stats (raw_snapshot_id);
CREATE INDEX source_coverage_scope_idx
    ON mirror.source_coverage (source, data_kind, observed_at DESC);
CREATE INDEX source_coverage_season_idx ON mirror.source_coverage (season_id);
CREATE INDEX source_coverage_competition_idx ON mirror.source_coverage (competition_id);
CREATE INDEX source_coverage_match_idx ON mirror.source_coverage (match_id);
CREATE INDEX source_coverage_raw_snapshot_idx ON mirror.source_coverage (raw_snapshot_id);
CREATE INDEX feature_snapshots_match_cutoff_idx
    ON engine.feature_snapshots (match_id, cutoff_at DESC);
CREATE INDEX feature_snapshots_schedule_revision_idx
    ON engine.feature_snapshots (schedule_revision_id);
CREATE INDEX prediction_attempts_job_idx ON engine.prediction_attempts (job_id);
CREATE INDEX prediction_attempts_snapshot_variant_idx
    ON engine.prediction_attempts (snapshot_id, variant_id, started_at DESC);
CREATE INDEX predictions_match_generated_idx
    ON engine.predictions (match_id, generated_at DESC);
CREATE INDEX predictions_schedule_revision_idx ON engine.predictions (schedule_revision_id);
CREATE INDEX predictions_variant_idx ON engine.predictions (variant_id);
CREATE INDEX predictions_market_snapshot_idx ON engine.predictions (market_snapshot_id);
CREATE INDEX prediction_events_prediction_time_idx
    ON engine.prediction_events (prediction_id, occurred_at, observed_at);
CREATE INDEX prediction_events_superseding_idx
    ON engine.prediction_events (superseding_prediction_id);
CREATE INDEX prediction_events_schedule_revision_idx
    ON engine.prediction_events (schedule_revision_id);
CREATE INDEX prediction_status_projection_event_idx
    ON engine.prediction_status_projection (latest_event_id);
CREATE INDEX market_snapshots_match_quote_idx
    ON market.market_snapshots (match_id, quoted_at DESC);
CREATE INDEX market_evaluations_market_snapshot_idx
    ON market.market_evaluations (market_snapshot_id);
CREATE INDEX evaluations_result_revision_idx ON engine.evaluations (result_revision_id);
CREATE INDEX jobs_claim_idx ON ops.jobs (state, due_at, deadline_at)
    WHERE state IN ('queued', 'running', 'retry_wait');
CREATE INDEX jobs_lease_idx ON ops.jobs (lease_until) WHERE lease_until IS NOT NULL;
CREATE INDEX job_attempts_started_idx ON ops.job_attempts (job_id, started_at DESC);
CREATE INDEX audit_events_object_idx
    ON ops.audit_events (object_type, object_id, occurred_at DESC);
CREATE INDEX audit_events_correlation_idx ON ops.audit_events (correlation_id);

CREATE FUNCTION ops.reject_append_only_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '55000',
        MESSAGE = format('%I.%I is append-only; %s is forbidden', TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP);
END
$function$;

CREATE FUNCTION ops.enforce_job_state_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF OLD.state = NEW.state THEN
        RETURN NEW;
    END IF;

    IF NOT (
        (OLD.state = 'queued' AND NEW.state IN (
            'running', 'cancelled', 'expired', 'quarantined'
        ))
        OR (OLD.state = 'running' AND NEW.state IN (
            'succeeded', 'failed', 'retry_wait', 'cancelled', 'expired', 'quarantined'
        ))
        OR (OLD.state = 'retry_wait' AND NEW.state IN (
            'running', 'cancelled', 'expired', 'quarantined'
        ))
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = format('invalid job state transition: %s -> %s', OLD.state, NEW.state);
    END IF;

    RETURN NEW;
END
$function$;

CREATE TRIGGER enforce_job_state_transition
BEFORE UPDATE OF state ON ops.jobs
FOR EACH ROW EXECUTE FUNCTION ops.enforce_job_state_transition();

DO $triggers$
DECLARE
    target regclass;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'mirror.raw_snapshots'::regclass,
        'mirror.seasons'::regclass,
        'mirror.competitions'::regclass,
        'mirror.franchises'::regclass,
        'mirror.team_identities'::regclass,
        'mirror.players'::regclass,
        'mirror.venues'::regclass,
        'mirror.matches'::regclass,
        'mirror.match_revisions'::regclass,
        'mirror.roster_revisions'::regclass,
        'mirror.result_revisions'::regclass,
        'mirror.match_sets'::regclass,
        'mirror.team_match_stats'::regclass,
        'mirror.player_match_stats'::regclass,
        'mirror.source_coverage'::regclass,
        'engine.feature_snapshots'::regclass,
        'engine.model_variants'::regclass,
        'engine.prediction_attempts'::regclass,
        'engine.predictions'::regclass,
        'engine.prediction_events'::regclass,
        'engine.evaluations'::regclass,
        'market.market_snapshots'::regclass,
        'market.market_evaluations'::regclass,
        'ops.job_attempts'::regclass,
        'ops.audit_events'::regclass
    ]
    LOOP
        EXECUTE format(
            'CREATE TRIGGER reject_append_only_row_mutation BEFORE UPDATE OR DELETE ON %s '
            'FOR EACH ROW EXECUTE FUNCTION ops.reject_append_only_mutation()',
            target
        );
        EXECUTE format(
            'CREATE TRIGGER reject_append_only_truncate BEFORE TRUNCATE ON %s '
            'FOR EACH STATEMENT EXECUTE FUNCTION ops.reject_append_only_mutation()',
            target
        );
    END LOOP;
END
$triggers$;

REVOKE ALL ON SCHEMA mirror, engine, market, ops FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA mirror, engine, market, ops FROM PUBLIC;

GRANT USAGE ON SCHEMA mirror TO vlytics_collector;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA mirror TO vlytics_collector;

GRANT USAGE ON SCHEMA mirror, engine, market, ops TO vlytics_engine;
GRANT SELECT ON ALL TABLES IN SCHEMA mirror, market TO vlytics_engine;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA engine, ops TO vlytics_engine;
GRANT INSERT ON market.market_evaluations TO vlytics_engine;
GRANT UPDATE (
    due_at,
    deadline_at,
    state,
    lease_owner,
    lease_until,
    attempt_no,
    error_code,
    updated_at
) ON ops.jobs TO vlytics_engine;
GRANT UPDATE, DELETE ON engine.prediction_status_projection TO vlytics_engine;

GRANT USAGE ON SCHEMA mirror, market TO vlytics_market_ingest;
GRANT SELECT ON mirror.matches TO vlytics_market_ingest;
GRANT SELECT, INSERT ON market.market_snapshots TO vlytics_market_ingest;

GRANT USAGE ON SCHEMA mirror, engine, market, ops TO vlytics_read_api;
GRANT SELECT ON ALL TABLES IN SCHEMA mirror, engine, market, ops TO vlytics_read_api;

COMMENT ON TABLE engine.prediction_status_projection IS
    'Mutable cache rebuilt exclusively from append-only engine.prediction_events';
COMMENT ON COLUMN mirror.matches.source_match_code IS
    'External source identifier retained as text; never used as the internal primary key';

RESET ROLE;

COMMENT ON ROLE vlytics_migration_owner IS 'Owns Vlytics schemas; not a login role';
COMMENT ON ROLE vlytics_collector IS 'Collector write boundary for mirror facts';
COMMENT ON ROLE vlytics_engine IS 'Prediction, evaluation, job, and projection boundary';
COMMENT ON ROLE vlytics_market_ingest IS 'Market snapshot ingestion boundary';
COMMENT ON ROLE vlytics_read_api IS 'Read-only API boundary';
