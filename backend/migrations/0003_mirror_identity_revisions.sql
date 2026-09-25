SET ROLE vlytics_migration_owner;

ALTER TABLE mirror.seasons ALTER COLUMN label DROP NOT NULL;
ALTER TABLE mirror.competitions ALTER COLUMN label DROP NOT NULL;
ALTER TABLE mirror.team_identities ALTER COLUMN display_name DROP NOT NULL;

ALTER TABLE mirror.result_revisions
    ADD COLUMN rule_version text NOT NULL DEFAULT 'legacy-unverified';
ALTER TABLE mirror.result_revisions ALTER COLUMN rule_version DROP DEFAULT;
ALTER TABLE mirror.result_revisions
    ADD CONSTRAINT result_revisions_rule_version_nonempty
    CHECK (btrim(rule_version) <> '');

ALTER TABLE mirror.source_coverage
    ADD COLUMN source_group_code text,
    ADD COLUMN source_season_code text,
    ADD COLUMN source_competition_code text,
    ADD COLUMN source_match_code text;
ALTER TABLE mirror.source_coverage
    ADD CONSTRAINT source_coverage_group_code_nonempty
        CHECK (source_group_code IS NULL OR btrim(source_group_code) <> ''),
    ADD CONSTRAINT source_coverage_season_code_nonempty
        CHECK (source_season_code IS NULL OR btrim(source_season_code) <> ''),
    ADD CONSTRAINT source_coverage_competition_code_nonempty
        CHECK (source_competition_code IS NULL OR btrim(source_competition_code) <> ''),
    ADD CONSTRAINT source_coverage_match_code_nonempty
        CHECK (source_match_code IS NULL OR btrim(source_match_code) <> '');

CREATE TABLE mirror.season_identity_revisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    season_id uuid NOT NULL REFERENCES mirror.seasons(id),
    revision integer NOT NULL CHECK (revision > 0),
    display_label text,
    observed_at timestamptz NOT NULL,
    raw_snapshot_id uuid NOT NULL REFERENCES mirror.raw_snapshots(id),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (season_id, revision),
    CHECK (display_label IS NULL OR btrim(display_label) <> '')
);

CREATE TABLE mirror.competition_identity_revisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    competition_id uuid NOT NULL REFERENCES mirror.competitions(id),
    revision integer NOT NULL CHECK (revision > 0),
    display_label text,
    stage text NOT NULL CHECK (stage IN ('regular', 'playoff', 'championship', 'other')),
    mapping_version text NOT NULL CHECK (btrim(mapping_version) <> ''),
    observed_at timestamptz NOT NULL,
    raw_snapshot_id uuid NOT NULL REFERENCES mirror.raw_snapshots(id),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (competition_id, revision),
    CHECK (display_label IS NULL OR btrim(display_label) <> '')
);

CREATE TABLE mirror.team_identity_revisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    team_identity_id uuid NOT NULL REFERENCES mirror.team_identities(id),
    revision integer NOT NULL CHECK (revision > 0),
    franchise_id uuid NOT NULL REFERENCES mirror.franchises(id),
    display_name text,
    mapping_version text NOT NULL CHECK (btrim(mapping_version) <> ''),
    evidence text NOT NULL CHECK (btrim(evidence) <> ''),
    observed_at timestamptz NOT NULL,
    raw_snapshot_id uuid NOT NULL REFERENCES mirror.raw_snapshots(id),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (team_identity_id, revision),
    CHECK (display_name IS NULL OR btrim(display_name) <> '')
);

CREATE TABLE mirror.player_identity_revisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    player_id uuid NOT NULL REFERENCES mirror.players(id),
    revision integer NOT NULL CHECK (revision > 0),
    display_label text,
    observed_at timestamptz NOT NULL,
    raw_snapshot_id uuid NOT NULL REFERENCES mirror.raw_snapshots(id),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (player_id, revision),
    CHECK (display_label IS NULL OR btrim(display_label) <> '')
);

CREATE INDEX season_identity_revisions_latest_idx
    ON mirror.season_identity_revisions (season_id, revision DESC);
CREATE INDEX competition_identity_revisions_latest_idx
    ON mirror.competition_identity_revisions (competition_id, revision DESC);
CREATE INDEX team_identity_revisions_latest_idx
    ON mirror.team_identity_revisions (team_identity_id, revision DESC);
CREATE INDEX player_identity_revisions_latest_idx
    ON mirror.player_identity_revisions (player_id, revision DESC);
CREATE INDEX source_coverage_source_scope_idx
    ON mirror.source_coverage (
        source,
        source_group_code,
        source_season_code,
        source_competition_code,
        source_match_code,
        observed_at DESC
    );

DO $triggers$
DECLARE
    target regclass;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'mirror.season_identity_revisions'::regclass,
        'mirror.competition_identity_revisions'::regclass,
        'mirror.team_identity_revisions'::regclass,
        'mirror.player_identity_revisions'::regclass
    ]
    LOOP
        EXECUTE format(
            'CREATE TRIGGER reject_append_only_row_mutation BEFORE UPDATE OR DELETE ON %s '
            'FOR EACH ROW EXECUTE FUNCTION ops.reject_append_only_mutation()',
            target
        );
        EXECUTE format(
            'CREATE TRIGGER reject_append_only_truncate BEFORE TRUNCATE ON %s '
            'FOR EACH STATEMENT EXECUTE FUNCTION ops.reject_append_only_mutation()',
            target
        );
    END LOOP;
END
$triggers$;

GRANT SELECT, INSERT ON
    mirror.season_identity_revisions,
    mirror.competition_identity_revisions,
    mirror.team_identity_revisions,
    mirror.player_identity_revisions
TO vlytics_collector;

GRANT SELECT ON
    mirror.season_identity_revisions,
    mirror.competition_identity_revisions,
    mirror.team_identity_revisions,
    mirror.player_identity_revisions
TO vlytics_engine, vlytics_read_api;

COMMENT ON TABLE mirror.team_identity_revisions IS
    'Verified append-only franchise mapping and display history; latest revision is authoritative';
COMMENT ON COLUMN mirror.source_coverage.source_match_code IS
    'Raw match scope retained even when OP-006 quarantine prevents creation of mirror.matches';

RESET ROLE;
