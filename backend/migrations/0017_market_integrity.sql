DO $migration$
BEGIN
    IF current_user <> 'vlytics_migrator' THEN
        RAISE EXCEPTION 'migrations must run as vlytics_migrator, got %', current_user;
    END IF;
END
$migration$;

SET LOCAL ROLE vlytics_migration_owner;

CREATE TABLE market.source_event_mappings (
    source text NOT NULL CHECK (btrim(source) <> ''),
    source_event_id text NOT NULL CHECK (btrim(source_event_id) <> ''),
    match_id uuid NOT NULL REFERENCES mirror.matches(id),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (source, source_event_id),
    UNIQUE (source, source_event_id, match_id)
);

DO $mapping_conflicts$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM market.market_snapshots
        GROUP BY source, source_event_id
        HAVING count(DISTINCT match_id) > 1
    ) THEN
        RAISE EXCEPTION 'existing Market source events map to multiple matches';
    END IF;
END
$mapping_conflicts$;

INSERT INTO market.source_event_mappings (source, source_event_id, match_id)
SELECT DISTINCT source, source_event_id, match_id
FROM market.market_snapshots;

ALTER TABLE market.market_snapshots
    ADD CONSTRAINT market_snapshots_quote_time_order
        CHECK (quoted_at <= observed_at AND observed_at <= received_at),
    ADD CONSTRAINT market_snapshots_source_event_mapping_fk
        FOREIGN KEY (source, source_event_id, match_id)
        REFERENCES market.source_event_mappings (source, source_event_id, match_id);

CREATE TABLE market.market_snapshot_lines (
    market_snapshot_id uuid NOT NULL REFERENCES market.market_snapshots(id),
    line_id text NOT NULL CHECK (btrim(line_id) <> ''),
    line_json jsonb NOT NULL CHECK (jsonb_typeof(line_json) = 'object'),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (market_snapshot_id, line_id),
    CHECK (line_json ->> 'line_id' = line_id)
);

INSERT INTO market.market_snapshot_lines (market_snapshot_id, line_id, line_json)
SELECT DISTINCT ON (snapshot.id, line ->> 'line_id')
       snapshot.id, line ->> 'line_id', line
FROM market.market_snapshots AS snapshot
CROSS JOIN LATERAL jsonb_array_elements(
    CASE
        WHEN jsonb_typeof(snapshot.markets_json -> 'markets') = 'array'
            THEN snapshot.markets_json -> 'markets'
        ELSE '[]'::jsonb
    END
) AS line
WHERE jsonb_typeof(line) = 'object'
  AND btrim(line ->> 'line_id') <> ''
ORDER BY snapshot.id, line ->> 'line_id';

ALTER TABLE market.market_settlements
    ADD CONSTRAINT market_settlements_snapshot_line_fk
        FOREIGN KEY (market_snapshot_id, line_id)
        REFERENCES market.market_snapshot_lines (market_snapshot_id, line_id);

CREATE INDEX market_snapshots_match_received_idx
    ON market.market_snapshots (match_id, received_at DESC);

CREATE TRIGGER reject_append_only_row_mutation
    BEFORE UPDATE OR DELETE ON market.source_event_mappings
    FOR EACH ROW EXECUTE FUNCTION ops.reject_append_only_mutation();

CREATE TRIGGER reject_append_only_truncate
    BEFORE TRUNCATE ON market.source_event_mappings
    FOR EACH STATEMENT EXECUTE FUNCTION ops.reject_append_only_mutation();

CREATE TRIGGER reject_append_only_row_mutation
    BEFORE UPDATE OR DELETE ON market.market_snapshot_lines
    FOR EACH ROW EXECUTE FUNCTION ops.reject_append_only_mutation();

CREATE TRIGGER reject_append_only_truncate
    BEFORE TRUNCATE ON market.market_snapshot_lines
    FOR EACH STATEMENT EXECUTE FUNCTION ops.reject_append_only_mutation();

REVOKE ALL PRIVILEGES ON market.source_event_mappings, market.market_snapshot_lines FROM PUBLIC;
GRANT SELECT, INSERT ON market.source_event_mappings, market.market_snapshot_lines
    TO vlytics_market_ingest;
GRANT SELECT ON market.source_event_mappings, market.market_snapshot_lines
    TO vlytics_engine, vlytics_read_api;

COMMENT ON TABLE market.source_event_mappings IS
    'Append-only external source event to internal match identity mapping';
COMMENT ON TABLE market.market_snapshot_lines IS
    'Authoritative normalized Market line membership for settlement foreign keys';

RESET ROLE;
