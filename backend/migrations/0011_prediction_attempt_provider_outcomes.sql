SET ROLE vlytics_migration_owner;

ALTER TABLE engine.prediction_attempts
    DROP CONSTRAINT IF EXISTS prediction_attempts_status_check;

ALTER TABLE engine.prediction_attempts
    ADD CONSTRAINT prediction_attempts_status_check
    CHECK (
        status IN (
            'succeeded',
            'failed',
            'timed_out',
            'budget_skipped',
            'skipped',
            'late_rejected'
        )
    );

COMMENT ON COLUMN engine.prediction_attempts.status IS
    'Immutable provider attempt outcome, including preflight config/input skips';

RESET ROLE;
