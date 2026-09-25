DO $migration$
BEGIN
    IF current_user <> 'vlytics_migrator' THEN
        RAISE EXCEPTION 'migrations must run as vlytics_migrator, got %', current_user;
    END IF;
END
$migration$;

SET LOCAL ROLE vlytics_migration_owner;

ALTER TABLE market.market_evaluations
    DISABLE TRIGGER reject_append_only_row_mutation;

UPDATE market.market_evaluations
SET eligibility = 'unsupported'
WHERE eligibility = 'ineligible';

ALTER TABLE market.market_evaluations
    ENABLE TRIGGER reject_append_only_row_mutation;

DO $constraints$
DECLARE
    constraint_name text;
BEGIN
    SELECT conname
    INTO constraint_name
    FROM pg_constraint
    WHERE conrelid = 'market.market_evaluations'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%eligibility%';

    IF constraint_name IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE market.market_evaluations DROP CONSTRAINT %I',
            constraint_name
        );
    END IF;

    SELECT conname
    INTO constraint_name
    FROM pg_constraint
    WHERE conrelid = 'market.market_evaluations'::regclass
      AND contype = 'u'
      AND pg_get_constraintdef(oid) =
          'UNIQUE (prediction_id, market_snapshot_id, evaluator_version)';

    IF constraint_name IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE market.market_evaluations DROP CONSTRAINT %I',
            constraint_name
        );
    END IF;
END
$constraints$;

ALTER TABLE market.market_evaluations
    ALTER COLUMN market_snapshot_id DROP NOT NULL,
    ADD CONSTRAINT market_evaluations_eligibility_check
        CHECK (eligibility IN ('eligible', 'missing', 'stale', 'late', 'unsupported')),
    ADD CONSTRAINT market_evaluations_identity_unique
        UNIQUE NULLS NOT DISTINCT (prediction_id, market_snapshot_id, evaluator_version);

COMMENT ON COLUMN market.market_evaluations.eligibility IS
    'Exact Market evaluation state: eligible, missing, stale, late, or unsupported';

RESET ROLE;
