DO $migration$
BEGIN
    IF current_user <> 'vlytics_migrator' THEN
        RAISE EXCEPTION 'migrations must run as vlytics_migrator, got %', current_user;
    END IF;
END
$migration$;

SET LOCAL ROLE vlytics_migration_owner;

CREATE TABLE market.market_settlements (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    match_id uuid NOT NULL REFERENCES mirror.matches(id),
    market_snapshot_id uuid NOT NULL REFERENCES market.market_snapshots(id),
    line_id text NOT NULL CHECK (btrim(line_id) <> ''),
    result_revision_id uuid NOT NULL REFERENCES mirror.result_revisions(id),
    evaluator_version text NOT NULL CHECK (btrim(evaluator_version) <> ''),
    outcome text NOT NULL CHECK (outcome IN ('win', 'loss', 'push', 'void', 'unsupported')),
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (market_snapshot_id, line_id, result_revision_id, evaluator_version),
    FOREIGN KEY (market_snapshot_id, match_id)
        REFERENCES market.market_snapshots(id, match_id),
    FOREIGN KEY (result_revision_id, match_id)
        REFERENCES mirror.result_revisions(id, match_id)
);

CREATE INDEX market_settlements_result_revision_idx
    ON market.market_settlements (result_revision_id);

CREATE TRIGGER reject_append_only_row_mutation
    BEFORE UPDATE OR DELETE ON market.market_settlements
    FOR EACH ROW EXECUTE FUNCTION ops.reject_append_only_mutation();

CREATE TRIGGER reject_append_only_truncate
    BEFORE TRUNCATE ON market.market_settlements
    FOR EACH STATEMENT EXECUTE FUNCTION ops.reject_append_only_mutation();

REVOKE ALL PRIVILEGES ON market.market_settlements FROM PUBLIC;
GRANT SELECT, INSERT ON market.market_settlements TO vlytics_engine;
GRANT SELECT ON market.market_settlements TO vlytics_read_api;

COMMENT ON TABLE market.market_settlements IS
    'Append-only Market line settlements keyed to the exact immutable result revision';

RESET ROLE;
