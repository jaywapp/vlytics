SET ROLE vlytics_migration_owner;

CREATE TABLE ops.sync_checkpoints (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source text NOT NULL CHECK (btrim(source) <> ''),
    source_group_code text NOT NULL CHECK (btrim(source_group_code) <> ''),
    source_season_code text NOT NULL CHECK (btrim(source_season_code) <> ''),
    source_competition_code text NOT NULL CHECK (btrim(source_competition_code) <> ''),
    cursor text,
    phase text NOT NULL CHECK (phase IN ('validation', 'batch', 'completed')),
    completed boolean NOT NULL DEFAULT false,
    pages_completed integer NOT NULL DEFAULT 0 CHECK (pages_completed >= 0),
    responses_ingested integer NOT NULL DEFAULT 0 CHECK (responses_ingested >= 0),
    updated_at timestamptz NOT NULL,
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    UNIQUE (
        source,
        source_group_code,
        source_season_code,
        source_competition_code
    ),
    CHECK (completed = (phase = 'completed')),
    CHECK (cursor IS NULL OR btrim(cursor) <> '')
);

CREATE FUNCTION ops.enforce_checkpoint_progress()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.generation <> OLD.generation + 1
       OR NEW.updated_at < OLD.updated_at
       OR NEW.pages_completed < OLD.pages_completed
       OR NEW.responses_ingested < OLD.responses_ingested
       OR (OLD.phase = 'batch' AND NEW.phase = 'validation')
       OR (OLD.phase = 'completed' AND NEW.phase <> 'completed')
       OR OLD.completed THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'sync checkpoint cannot move backwards or reopen';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER enforce_checkpoint_progress
BEFORE UPDATE ON ops.sync_checkpoints
FOR EACH ROW EXECUTE FUNCTION ops.enforce_checkpoint_progress();

GRANT USAGE ON SCHEMA ops TO vlytics_collector;
GRANT SELECT, INSERT ON ops.sync_checkpoints TO vlytics_collector;
GRANT UPDATE (
    cursor,
    phase,
    completed,
    pages_completed,
    responses_ingested,
    updated_at,
    generation
) ON ops.sync_checkpoints TO vlytics_collector;

GRANT SELECT, INSERT ON ops.sync_checkpoints TO vlytics_engine;
GRANT UPDATE (
    cursor,
    phase,
    completed,
    pages_completed,
    responses_ingested,
    updated_at,
    generation
) ON ops.sync_checkpoints TO vlytics_engine;

GRANT SELECT ON ops.sync_checkpoints TO vlytics_read_api;

COMMENT ON TABLE ops.sync_checkpoints IS
    'Mutable, monotonic cursor for resumable season and competition collection';

RESET ROLE;
