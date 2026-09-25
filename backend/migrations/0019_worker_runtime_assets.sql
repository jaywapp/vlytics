DO $migration$
BEGIN
    IF current_user <> 'vlytics_migrator' THEN
        RAISE EXCEPTION 'migrations must run as vlytics_migrator, got %', current_user;
    END IF;
END
$migration$;

SET LOCAL ROLE vlytics_migration_owner;

CREATE TABLE engine.joint_score_distributions (
    distribution_id text PRIMARY KEY CHECK (distribution_id ~ '^[a-f0-9]{64}$'),
    match_id uuid NOT NULL REFERENCES mirror.matches(id),
    snapshot_id uuid NOT NULL REFERENCES engine.feature_snapshots(id),
    variant_id uuid NOT NULL REFERENCES engine.model_variants(id),
    home_win_probability double precision NOT NULL
        CHECK (home_win_probability BETWEEN 0 AND 1),
    set_score_probabilities jsonb NOT NULL
        CHECK (
            jsonb_typeof(set_score_probabilities) = 'array'
            AND jsonb_array_length(set_score_probabilities) = 6
        ),
    artifact_json jsonb NOT NULL CHECK (jsonb_typeof(artifact_json) = 'object'),
    sha256 text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (snapshot_id, variant_id, distribution_id)
);

CREATE INDEX joint_score_distributions_match_idx
    ON engine.joint_score_distributions (match_id, created_at DESC);

CREATE TRIGGER reject_append_only_row_mutation
    BEFORE UPDATE OR DELETE ON engine.joint_score_distributions
    FOR EACH ROW EXECUTE FUNCTION ops.reject_append_only_mutation();

CREATE TRIGGER reject_append_only_truncate
    BEFORE TRUNCATE ON engine.joint_score_distributions
    FOR EACH STATEMENT EXECUTE FUNCTION ops.reject_append_only_mutation();

GRANT SELECT, INSERT ON engine.joint_score_distributions TO vlytics_engine;
GRANT SELECT ON engine.joint_score_distributions TO vlytics_read_api;
COMMENT ON TABLE engine.joint_score_distributions IS
    'Append-only server-owned joint score artifacts referenced by prediction-v1 outputs';

RESET ROLE;

GRANT SELECT ON public.vlytics_schema_migrations
    TO vlytics_collector, vlytics_engine, vlytics_market_ingest, vlytics_read_api;
