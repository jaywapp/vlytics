DO $migration$
BEGIN
    IF current_user <> 'vlytics_migrator' THEN
        RAISE EXCEPTION 'migrations must run as vlytics_migrator, got %', current_user;
    END IF;
END
$migration$;

SET LOCAL ROLE vlytics_migration_owner;

ALTER TABLE ops.jobs
    ADD COLUMN lease_started_at timestamptz,
    ADD COLUMN match_id uuid REFERENCES mirror.matches(id),
    ADD COLUMN schedule_revision_id uuid,
    ADD COLUMN stage text,
    ADD COLUMN variant_id uuid REFERENCES engine.model_variants(id);

UPDATE ops.jobs
SET lease_started_at = updated_at
WHERE state = 'running';

ALTER TABLE ops.jobs
    ADD CONSTRAINT jobs_schedule_revision_match_fk
        FOREIGN KEY (schedule_revision_id, match_id)
        REFERENCES mirror.match_revisions(id, match_id),
    ADD CONSTRAINT jobs_lease_started_state_check
        CHECK ((state = 'running') = (lease_started_at IS NOT NULL)),
    ADD CONSTRAINT jobs_schedule_identity_check
        CHECK (
            (schedule_revision_id IS NULL AND match_id IS NULL AND stage IS NULL
             AND variant_id IS NULL)
            OR
            (schedule_revision_id IS NOT NULL AND match_id IS NOT NULL
             AND stage IS NOT NULL AND btrim(stage) <> '')
        );

CREATE INDEX jobs_schedule_revision_idx
    ON ops.jobs (schedule_revision_id, stage, variant_id);

CREATE TABLE ops.schedule_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_key text NOT NULL UNIQUE CHECK (btrim(event_key) <> ''),
    match_id uuid NOT NULL REFERENCES mirror.matches(id),
    previous_schedule_revision_id uuid,
    schedule_revision_id uuid NOT NULL,
    event_type text NOT NULL
        CHECK (event_type IN ('postponed', 'cancelled', 'rescheduled', 'earlier_start')),
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    occurred_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    FOREIGN KEY (previous_schedule_revision_id, match_id)
        REFERENCES mirror.match_revisions(id, match_id),
    FOREIGN KEY (schedule_revision_id, match_id)
        REFERENCES mirror.match_revisions(id, match_id)
);

CREATE INDEX schedule_events_match_time_idx
    ON ops.schedule_events (match_id, occurred_at DESC);

CREATE TRIGGER reject_append_only_row_mutation
    BEFORE UPDATE OR DELETE ON ops.schedule_events
    FOR EACH ROW EXECUTE FUNCTION ops.reject_append_only_mutation();

CREATE TRIGGER reject_append_only_truncate
    BEFORE TRUNCATE ON ops.schedule_events
    FOR EACH STATEMENT EXECUTE FUNCTION ops.reject_append_only_mutation();

CREATE UNIQUE INDEX prediction_events_one_published_idx
    ON engine.prediction_events (prediction_id)
    WHERE event_type = 'published';

CREATE UNIQUE INDEX prediction_events_one_late_rejected_idx
    ON engine.prediction_events (prediction_id)
    WHERE event_type = 'late_rejected';

GRANT SELECT, INSERT ON ops.schedule_events TO vlytics_engine;
GRANT SELECT ON ops.schedule_events TO vlytics_read_api;
GRANT UPDATE (
    lease_started_at,
    match_id,
    schedule_revision_id,
    stage,
    variant_id
) ON ops.jobs TO vlytics_engine;

COMMENT ON TABLE ops.schedule_events IS
    'Append-only evidence for postponement, cancellation, and schedule-time changes';
COMMENT ON COLUMN ops.jobs.lease_started_at IS
    'Start time copied into the immutable job-attempt row when a lease is completed or recovered';

RESET ROLE;
