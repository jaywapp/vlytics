DO $migration$
BEGIN
    IF current_user <> 'vlytics_migrator' THEN
        RAISE EXCEPTION 'migrations must run as vlytics_migrator, got %', current_user;
    END IF;
END
$migration$;

SET LOCAL ROLE vlytics_migration_owner;

CREATE FUNCTION ops.lock_match_schedule_revision()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('match-schedule:' || NEW.match_id::text, 0)
    );
    RETURN NEW;
END
$function$;

CREATE TRIGGER lock_match_schedule_revision
    BEFORE INSERT ON mirror.match_revisions
    FOR EACH ROW EXECUTE FUNCTION ops.lock_match_schedule_revision();

CREATE UNIQUE INDEX jobs_schedule_semantic_variant_idx
    ON ops.jobs (job_type, schedule_revision_id, stage, variant_id)
    WHERE schedule_revision_id IS NOT NULL
      AND variant_id IS NOT NULL
      AND job_type = 'engine.run_prediction';

CREATE UNIQUE INDEX jobs_schedule_semantic_stage_idx
    ON ops.jobs (job_type, schedule_revision_id, stage)
    WHERE schedule_revision_id IS NOT NULL
      AND variant_id IS NULL
      AND job_type = 'engine.freeze_snapshot';

CREATE TABLE ops.provider_budget_reservations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reservation_key text NOT NULL UNIQUE CHECK (btrim(reservation_key) <> ''),
    provider text NOT NULL CHECK (btrim(provider) <> ''),
    job_id uuid NOT NULL REFERENCES ops.jobs(id),
    reserved_at timestamptz NOT NULL,
    budget_day date NOT NULL,
    budget_month date NOT NULL CHECK (date_trunc('month', budget_month)::date = budget_month),
    reserved_amount numeric(20, 8) NOT NULL CHECK (reserved_amount >= 0),
    settled_amount numeric(20, 8) CHECK (settled_amount >= 0),
    state text NOT NULL DEFAULT 'reserved' CHECK (state IN ('reserved', 'settled')),
    conservative_charge boolean NOT NULL DEFAULT false,
    settled_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CHECK ((state = 'settled') = (settled_amount IS NOT NULL)),
    CHECK ((state = 'settled') = (settled_at IS NOT NULL))
);

CREATE INDEX provider_budget_daily_idx
    ON ops.provider_budget_reservations (provider, budget_day);

CREATE INDEX provider_budget_monthly_idx
    ON ops.provider_budget_reservations (provider, budget_month);

CREATE TRIGGER reject_provider_budget_delete
    BEFORE DELETE OR TRUNCATE ON ops.provider_budget_reservations
    FOR EACH STATEMENT EXECUTE FUNCTION ops.reject_append_only_mutation();

GRANT SELECT, INSERT, UPDATE (settled_amount, state, conservative_charge, settled_at)
    ON ops.provider_budget_reservations TO vlytics_engine;

COMMENT ON TABLE ops.provider_budget_reservations IS
    'Durable atomic provider call and cost reservations; unknown outcomes settle conservatively';
COMMENT ON FUNCTION ops.lock_match_schedule_revision() IS
    'Serializes immutable schedule revision inserts with final prediction publication checks';

RESET ROLE;
