SET ROLE vlytics_migration_owner;

DO $migration$
DECLARE
    constraint_name text;
BEGIN
    SELECT target_constraint.conname
    INTO constraint_name
    FROM pg_constraint AS target_constraint
    WHERE target_constraint.conrelid = 'engine.feature_snapshots'::regclass
      AND target_constraint.contype = 'u'
      AND (
          SELECT array_agg(attribute.attname::text ORDER BY key_columns.ordinality)
          FROM unnest(target_constraint.conkey)
              WITH ORDINALITY AS key_columns(attnum, ordinality)
          JOIN pg_attribute AS attribute
            ON attribute.attrelid = target_constraint.conrelid
           AND attribute.attnum = key_columns.attnum
      ) = ARRAY[
          'match_id',
          'schedule_revision_id',
          'feature_version',
          'cutoff_at'
      ];

    IF constraint_name IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE engine.feature_snapshots DROP CONSTRAINT %I',
            constraint_name
        );
    END IF;
END
$migration$;

ALTER TABLE engine.feature_snapshots
    ADD CONSTRAINT feature_snapshots_policy_identity_key UNIQUE (
        match_id,
        schedule_revision_id,
        feature_version,
        cutoff_at,
        availability_policy
    );

COMMENT ON CONSTRAINT feature_snapshots_policy_identity_key
    ON engine.feature_snapshots IS
    'Availability cohorts are distinct immutable feature snapshots';

RESET ROLE;
