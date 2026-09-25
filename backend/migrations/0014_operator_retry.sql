DO $migration$
BEGIN
    IF current_user <> 'vlytics_migrator' THEN
        RAISE EXCEPTION 'migrations must run as vlytics_migrator, got %', current_user;
    END IF;
END
$migration$;

SET LOCAL ROLE vlytics_migration_owner;

CREATE TABLE ops.operator_retry_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key text NOT NULL UNIQUE
        CHECK (btrim(idempotency_key) <> '' AND length(idempotency_key) <= 200),
    job_id uuid NOT NULL REFERENCES ops.jobs(id),
    requested_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT statement_timestamp()
);

CREATE TRIGGER reject_append_only_row_mutation
    BEFORE UPDATE OR DELETE ON ops.operator_retry_requests
    FOR EACH ROW EXECUTE FUNCTION ops.reject_append_only_mutation();

CREATE TRIGGER reject_append_only_truncate
    BEFORE TRUNCATE ON ops.operator_retry_requests
    FOR EACH STATEMENT EXECUTE FUNCTION ops.reject_append_only_mutation();

CREATE OR REPLACE FUNCTION ops.enforce_job_state_transition()
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
        OR (OLD.state = 'failed' AND NEW.state = 'retry_wait')
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = format('invalid job state transition: %s -> %s', OLD.state, NEW.state);
    END IF;

    RETURN NEW;
END
$function$;

CREATE FUNCTION ops.request_job_retry(
    requested_job_id uuid,
    requested_idempotency_key text,
    requested_at timestamptz
)
RETURNS TABLE (
    outcome text,
    job_id uuid,
    state text,
    due_at timestamptz,
    deadline_at timestamptz,
    idempotent_replay boolean
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    previous_request ops.operator_retry_requests%ROWTYPE;
    target_job ops.jobs%ROWTYPE;
BEGIN
    IF requested_idempotency_key IS NULL
       OR btrim(requested_idempotency_key) = ''
       OR length(requested_idempotency_key) > 200 THEN
        RETURN QUERY SELECT 'idempotency_conflict', requested_job_id,
                            NULL::text, NULL::timestamptz, NULL::timestamptz, false;
        RETURN;
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended(requested_idempotency_key, 0));

    SELECT * INTO previous_request
    FROM ops.operator_retry_requests AS request
    WHERE request.idempotency_key = requested_idempotency_key;

    IF FOUND THEN
        IF previous_request.job_id <> requested_job_id THEN
            RETURN QUERY SELECT 'idempotency_conflict', requested_job_id,
                                NULL::text, NULL::timestamptz, NULL::timestamptz, false;
            RETURN;
        END IF;
        SELECT * INTO target_job FROM ops.jobs AS job WHERE job.id = requested_job_id;
        RETURN QUERY SELECT 'scheduled', requested_job_id, 'retry_wait'::text,
                            previous_request.requested_at, target_job.deadline_at, true;
        RETURN;
    END IF;

    SELECT * INTO target_job
    FROM ops.jobs AS job
    WHERE job.id = requested_job_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT 'job_not_found', requested_job_id,
                            NULL::text, NULL::timestamptz, NULL::timestamptz, false;
        RETURN;
    END IF;
    IF target_job.deadline_at IS NOT NULL AND target_job.deadline_at <= requested_at THEN
        RETURN QUERY SELECT 'retry_deadline_expired', requested_job_id,
                            target_job.state, target_job.due_at, target_job.deadline_at, false;
        RETURN;
    END IF;
    IF target_job.state <> 'failed' THEN
        RETURN QUERY SELECT 'job_not_retryable', requested_job_id,
                            target_job.state, target_job.due_at, target_job.deadline_at, false;
        RETURN;
    END IF;

    UPDATE ops.jobs AS job
    SET state = 'retry_wait',
        due_at = requested_at,
        error_code = NULL,
        updated_at = requested_at
    WHERE job.id = requested_job_id;

    INSERT INTO ops.operator_retry_requests (idempotency_key, job_id, requested_at)
    VALUES (requested_idempotency_key, requested_job_id, requested_at);

    RETURN QUERY SELECT 'scheduled', requested_job_id, 'retry_wait'::text,
                        requested_at, target_job.deadline_at, false;
END
$function$;

REVOKE ALL PRIVILEGES ON ops.operator_retry_requests FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION ops.request_job_retry(uuid, text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops.request_job_retry(uuid, text, timestamptz) TO vlytics_read_api;

COMMENT ON TABLE ops.operator_retry_requests IS
    'Append-only idempotency ledger for authenticated operator retry requests';
COMMENT ON FUNCTION ops.request_job_retry(uuid, text, timestamptz) IS
    'Deadline- and state-checked operator retry transition exposed through a restricted function';

RESET ROLE;
