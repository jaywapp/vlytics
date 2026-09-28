"""Production runtime adapters for the durable worker process."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import Connection, Engine, bindparam, text

from vlytics.config import OperationalConfigError
from vlytics.engine.features import (
    AsOfFeatureBuilder,
    AvailabilityPolicy,
    FeatureInputRepository,
    FeatureSnapshot,
)
from vlytics.engine.orchestrator import (
    MatchExecutionContext,
    ProviderBinding,
    StatisticalBinding,
    StatisticalPredictionResult,
)
from vlytics.engine.prediction_validation import JointScoreReferenceMetadata
from vlytics.engine.predictors import (
    DEFAULT_SCORE_RULE_VERSION,
    Division,
    EloConfig,
    EloMatch,
    EloResultRevision,
    FranchiseIdentity,
    JointScorePredictor,
    PointModelConfig,
    ResultFinality,
    ResultFinalityPolicy,
    SetModelConfig,
    SetOutcomePrediction,
    SetTrainingCohort,
    TimingEligibility,
    VerifiedSeasonScoreRule,
    VerifiedSetPredictionArtifact,
    independent_set_distribution,
    reproduction_candidate,
    solve_regular_set_probability,
)
from vlytics.engine.predictors.elo import EloReplayPrediction, replay_elo_prediction
from vlytics.engine.providers import LiveProviderPlan, PredictionProvider
from vlytics.engine.providers.models import canonical_json_bytes, sha256_bytes
from vlytics.ops.scheduler import JobHandler, TerminalJobError
from vlytics.storage.migrations import load_migrations

STATISTICAL_VARIANT_KEY = "statistical-joint-v1"
STATISTICAL_MODEL_VERSION = "elo-p5-joint-v2"
STATISTICAL_DISTRIBUTION_VERSION = "joint-score-v1"
_STATISTICAL_VARIANT_IDENTITY = "https://vlytics.local/variants/statistical-joint-elo-v2"
_ELO_CONFIG = reproduction_candidate()


def database_preflight(engine: Engine) -> str:
    """Verify the worker login and every packaged migration before polling."""

    expected = {migration.version: migration.checksum for migration in load_migrations()}
    try:
        with engine.connect() as connection:
            current_user = str(connection.execute(text("SELECT current_user")).scalar_one())
            if current_user != "vlytics_engine_login":
                raise OperationalConfigError(
                    "worker database URL must authenticate as vlytics_engine_login"
                )
            rows = connection.execute(
                text("SELECT version, checksum FROM public.vlytics_schema_migrations")
            ).all()
    except OperationalConfigError:
        raise
    except Exception as error:
        raise OperationalConfigError(
            "worker database connection or migration ledger check failed"
        ) from error
    applied = {str(version): str(checksum) for version, checksum in rows}
    missing = sorted(set(expected).difference(applied))
    changed = sorted(
        version
        for version, checksum in expected.items()
        if version in applied and applied[version] != checksum
    )
    unexpected = sorted(set(applied).difference(expected))
    if missing or changed or unexpected:
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if changed:
            details.append("checksum_mismatch=" + ",".join(changed))
        if unexpected:
            details.append("unknown=" + ",".join(unexpected))
        raise OperationalConfigError(
            "worker database migration revision is incompatible: " + "; ".join(details)
        )
    return current_user


class DisabledSourceJobHandler:
    """Record an explicit terminal state when collection is intentionally disabled."""

    def handle(self, job: Mapping[str, Any], *, now: datetime) -> None:
        del job, now
        raise TerminalJobError("source_collection_disabled_by_config")


def disabled_source_handlers() -> dict[str, JobHandler]:
    handler = DisabledSourceJobHandler()
    return {
        "mirror.pre_cutoff_sync": handler,
        "mirror.current_schedule": handler,
        "mirror.final_result": handler,
        "mirror.correction_recheck": handler,
    }


class DatabaseFeatureSnapshotFactory:
    """Build leakage-safe feature snapshots from verified mirror facts."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._builder = AsOfFeatureBuilder()

    def build(
        self,
        schedule: MatchExecutionContext,
        *,
        cutoff_at: datetime,
        captured_at: datetime,
    ) -> FeatureSnapshot:
        try:
            with self._engine.connect() as connection:
                inputs = FeatureInputRepository(connection).load(
                    match_id=schedule.match_id,
                    schedule_revision_id=schedule.schedule_revision_id,
                    cutoff_at=cutoff_at,
                )
            return self._builder.build(
                target=inputs.target,
                cutoff_at=cutoff_at,
                captured_at=captured_at,
                policy=AvailabilityPolicy.LIVE_PROSPECTIVE,
                matches=inputs.matches,
                roster_revisions=inputs.roster_revisions,
            )
        except ValueError as error:
            raise TerminalJobError("feature_snapshot_inputs_ineligible") from error


@dataclass(frozen=True)
class StatisticalMatchMetadata:
    source: str
    source_group_code: str
    season_id: str
    competition: str
    stage: str
    division: Division
    scheduled_start_at: datetime
    schedule_observed_at: datetime
    schedule_revision: int
    schedule_raw_snapshot_sha256: str


class DatabaseStatisticalPredictionRunner:
    """Produce set and verified joint-score outputs from one frozen snapshot."""

    def __init__(self, engine: Engine, *, variant_id: UUID, variant_key: str) -> None:
        self._engine = engine
        self._variant_id = variant_id
        self._variant_key = variant_key
        self._joint_lock = Lock()

    def predict(
        self,
        *,
        snapshot_id: UUID,
        snapshot: FeatureSnapshot,
    ) -> StatisticalPredictionResult:
        with self._engine.connect() as connection:
            metadata = self._metadata(connection, snapshot)
            elo_replay = self._elo_replay(connection, snapshot, metadata)
            fifth_set_home_wins, fifth_set_matches, result_ids = self._fifth_set_history(
                connection,
                snapshot,
                eligible_result_ids=elo_replay.applied_result_revision_ids,
            )
            verified_rule = self._verified_rule(
                connection,
                snapshot,
                metadata,
                eligible_result_ids=elo_replay.applied_result_revision_ids,
            )

        elo_prediction = elo_replay.prediction
        home_probability = elo_prediction.home_win_probability
        p5 = (fifth_set_home_wins + 1.0) / (fifth_set_matches + 2.0)
        regular_probability = solve_regular_set_probability(home_probability, p5)
        distribution = independent_set_distribution(regular_probability, p5)
        cohort = SetTrainingCohort(
            division=metadata.division,
            availability_policy=snapshot.availability_policy.value,
            competition=metadata.competition,
            stage=metadata.stage,
            timing_eligibility="on_time",
            result_finality_policy="final_only",
            input_version=elo_prediction.input_version,
            franchise_mapping_version=elo_prediction.franchise_mapping_version,
        )
        set_config = SetModelConfig()
        manifest = {
            "result_revision_ids": list(result_ids),
            "fifth_set_home_wins": fifth_set_home_wins,
            "fifth_set_matches": fifth_set_matches,
            "elo_applied_result_revision_ids": list(elo_replay.applied_result_revision_ids),
            "elo_input_manifest_sha256": elo_replay.input_manifest_sha256,
        }
        manifest_sha256 = sha256_bytes(canonical_json_bytes(manifest))
        fitted_id = sha256_bytes(
            canonical_json_bytes(
                {
                    "cohort": cohort.to_dict(),
                    "manifest_sha256": manifest_sha256,
                    "set_config_id": set_config.config_id,
                    "training_as_of": snapshot.cutoff_at.isoformat(),
                }
            )
        )
        upstream_config_id = elo_prediction.config_id
        upstream_prediction_id = sha256_bytes(
            canonical_json_bytes(
                {
                    "config_id": upstream_config_id,
                    "elo_input_manifest_sha256": elo_replay.input_manifest_sha256,
                    "home_rating": elo_prediction.home_rating,
                    "home_win_probability": home_probability,
                    "model_version": elo_prediction.model_version,
                    "snapshot_id": str(snapshot_id),
                    "away_rating": elo_prediction.away_rating,
                }
            )
        )
        prediction = SetOutcomePrediction(
            match_id=snapshot.target_match_id,
            schedule_revision_id=snapshot.schedule_revision_id,
            prediction_cutoff_at=snapshot.cutoff_at,
            division=metadata.division,
            source_home_win_probability=home_probability,
            source_prediction_id=upstream_prediction_id,
            source_model_version=elo_prediction.model_version,
            source_config_id=upstream_config_id,
            regular_set_home_win_probability=regular_probability,
            fifth_set_home_win_probability=p5,
            distribution=distribution,
            model_version=set_config.model_version,
            distribution_version=set_config.distribution_version,
            config_id=sha256_bytes(
                canonical_json_bytes(
                    {
                        "fitted_parameter_artifact_id": fitted_id,
                        "set_config_id": set_config.config_id,
                        "upstream_config_id": upstream_config_id,
                    }
                )
            ),
            random_seed=set_config.random_seed,
            capabilities=set_config.capabilities,
            fitted_parameter_artifact_id=fitted_id,
            training_cohort=cohort,
            training_as_of=snapshot.cutoff_at,
            training_result_manifest_sha256=manifest_sha256,
        )
        artifact = VerifiedSetPredictionArtifact.capture(
            prediction,
            producer_variant_id=self._variant_key,
            input_snapshot_id=str(snapshot_id),
        )
        if verified_rule is None:
            output = prediction.to_contract(
                producer_variant_id=self._variant_key,
                input_snapshot_id=str(snapshot_id),
            )
            output["risk_factors"] = [
                *cast(list[str], output["risk_factors"]),
                "verified_season_score_rule_unavailable_point_targets_omitted",
            ]
            return StatisticalPredictionResult(output, STATISTICAL_MODEL_VERSION)

        joint = JointScorePredictor(
            PointModelConfig(score_rule_version=verified_rule.rule_version),
            producer_variant_id=self._variant_key,
        ).predict(artifact, verified_rule)
        joint_document = joint.distribution.to_artifact()
        encoded = canonical_json_bytes(joint_document)
        with self._joint_lock, self._engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO engine.joint_score_distributions (
                        distribution_id, match_id, snapshot_id, variant_id,
                        home_win_probability, set_score_probabilities,
                        artifact_json, sha256
                    ) VALUES (
                        :distribution_id, :match_id, :snapshot_id, :variant_id,
                        :home_win_probability, CAST(:set_score_probabilities AS jsonb),
                        CAST(:artifact_json AS jsonb), :sha256
                    )
                    ON CONFLICT (distribution_id) DO NOTHING
                    """
                ),
                {
                    "distribution_id": joint.distribution.distribution_id,
                    "match_id": UUID(snapshot.target_match_id),
                    "snapshot_id": snapshot_id,
                    "variant_id": self._variant_id,
                    "home_win_probability": joint.distribution.home_win_probability,
                    "set_score_probabilities": json.dumps(
                        list(joint.distribution.set_score_distribution.probabilities)
                    ),
                    "artifact_json": encoded.decode("utf-8"),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                },
            )
        return StatisticalPredictionResult(
            joint.to_contract(
                producer_variant_id=self._variant_key,
                input_snapshot_id=str(snapshot_id),
            ),
            STATISTICAL_MODEL_VERSION,
        )

    def resolve(self, distribution_id: str) -> JointScoreReferenceMetadata | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        """
                        SELECT distribution_id, snapshot_id, home_win_probability,
                               set_score_probabilities
                        FROM engine.joint_score_distributions
                        WHERE distribution_id = :distribution_id
                          AND variant_id = :variant_id
                        """
                    ),
                    {"distribution_id": distribution_id, "variant_id": self._variant_id},
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        probabilities = row["set_score_probabilities"]
        if not isinstance(probabilities, list) or len(probabilities) != 6:
            raise RuntimeError("stored joint score marginal is malformed")
        return JointScoreReferenceMetadata(
            distribution_id=str(row["distribution_id"]),
            producer_variant_id=self._variant_key,
            input_snapshot_id=str(row["snapshot_id"]),
            home_win_probability=float(row["home_win_probability"]),
            set_score_probabilities=tuple(float(item) for item in probabilities),
        )

    @classmethod
    def _elo_replay(
        cls,
        connection: Connection,
        snapshot: FeatureSnapshot,
        metadata: StatisticalMatchMetadata,
        *,
        config: EloConfig = _ELO_CONFIG,
    ) -> EloReplayPrediction:
        try:
            timeline = cls._elo_timeline(connection, snapshot, metadata)
            return replay_elo_prediction(
                timeline,
                config,
                target_match_id=snapshot.target_match_id,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise TerminalJobError("statistical_elo_inputs_ineligible") from error

    @classmethod
    def _elo_timeline(
        cls,
        connection: Connection,
        snapshot: FeatureSnapshot,
        metadata: StatisticalMatchMetadata,
    ) -> tuple[EloMatch, ...]:
        cutoff_lead = metadata.scheduled_start_at - snapshot.cutoff_at
        if cutoff_lead < timedelta(0):
            raise ValueError("prediction cutoff cannot follow the scheduled start")
        rows = tuple(
            connection.execute(
                text(
                    """
                    WITH schedules AS (
                        SELECT DISTINCT ON (m.id)
                               m.id AS match_id, m.season_id,
                               mr.id AS schedule_revision_id,
                               mr.revision AS schedule_revision,
                               mr.observed_at AS schedule_observed_at,
                               mr.scheduled_start_at,
                               mr.scheduled_start_at - :cutoff_lead
                                   AS prediction_cutoff_at,
                               mr.home_team_id, mr.away_team_id,
                               raw.sha256 AS schedule_raw_sha256
                        FROM mirror.matches m
                        JOIN mirror.competitions c ON c.id = m.competition_id
                        JOIN mirror.match_revisions mr ON mr.match_id = m.id
                        JOIN mirror.raw_snapshots raw ON raw.id = mr.raw_snapshot_id
                        WHERE m.source = :source
                          AND m.source_group_code = :source_group_code
                          AND c.source_competition_code = :competition
                          AND c.division = :division
                          AND c.stage = :stage
                          AND mr.observed_at <= mr.scheduled_start_at - :cutoff_lead
                          AND mr.scheduled_start_at - :cutoff_lead <= :target_cutoff
                        ORDER BY m.id, mr.revision DESC, mr.observed_at DESC, mr.id DESC
                    )
                    SELECT schedules.*,
                           home_identity.franchise_id AS home_franchise_id,
                           home_identity.mapping_version AS home_mapping_version,
                           home_identity.evidence AS home_evidence,
                           away_identity.franchise_id AS away_franchise_id,
                           away_identity.mapping_version AS away_mapping_version,
                           away_identity.evidence AS away_evidence,
                           rr.id AS result_revision_id,
                           rr.revision AS result_revision,
                           rr.observed_at AS result_observed_at,
                           rr.finality AS result_finality,
                           rr.home_sets, rr.away_sets,
                           result_raw.sha256 AS result_raw_sha256
                    FROM schedules
                    LEFT JOIN LATERAL (
                        SELECT tir.franchise_id, tir.mapping_version, tir.evidence
                        FROM mirror.team_identity_revisions tir
                        WHERE tir.team_identity_id = schedules.home_team_id
                          AND tir.observed_at <= schedules.prediction_cutoff_at
                        ORDER BY tir.revision DESC, tir.observed_at DESC, tir.id DESC
                        LIMIT 1
                    ) home_identity ON true
                    LEFT JOIN LATERAL (
                        SELECT tir.franchise_id, tir.mapping_version, tir.evidence
                        FROM mirror.team_identity_revisions tir
                        WHERE tir.team_identity_id = schedules.away_team_id
                          AND tir.observed_at <= schedules.prediction_cutoff_at
                        ORDER BY tir.revision DESC, tir.observed_at DESC, tir.id DESC
                        LIMIT 1
                    ) away_identity ON true
                    LEFT JOIN LATERAL (
                        SELECT coverage.availability, coverage.raw_snapshot_id
                        FROM mirror.source_coverage coverage
                        WHERE coverage.match_id = schedules.match_id
                          AND coverage.data_kind = 'match_result'
                          AND coverage.observed_at <= :target_cutoff
                        ORDER BY coverage.observed_at DESC, coverage.created_at DESC,
                                 coverage.id DESC
                        LIMIT 1
                    ) latest_coverage ON true
                    LEFT JOIN mirror.result_revisions rr
                      ON rr.match_id = schedules.match_id
                     AND rr.observed_at <= :target_cutoff
                     AND latest_coverage.availability = 'available'
                     AND latest_coverage.raw_snapshot_id = (
                         SELECT latest_result.raw_snapshot_id
                         FROM mirror.result_revisions latest_result
                         WHERE latest_result.match_id = schedules.match_id
                           AND latest_result.observed_at <= :target_cutoff
                         ORDER BY latest_result.revision DESC,
                                  latest_result.observed_at DESC,
                                  latest_result.id DESC
                         LIMIT 1
                     )
                     AND EXISTS (
                         SELECT 1
                         FROM mirror.source_coverage accepted
                         WHERE accepted.match_id = rr.match_id
                           AND accepted.data_kind = 'match_result'
                           AND accepted.raw_snapshot_id = rr.raw_snapshot_id
                           AND accepted.availability = 'available'
                           AND accepted.observed_at <= :target_cutoff
                     )
                    LEFT JOIN mirror.raw_snapshots result_raw
                      ON result_raw.id = rr.raw_snapshot_id
                    ORDER BY schedules.prediction_cutoff_at, schedules.match_id,
                             rr.revision, rr.observed_at, rr.id
                    """
                ),
                {
                    "competition": metadata.competition,
                    "cutoff_lead": cutoff_lead,
                    "division": metadata.division.value,
                    "source": metadata.source,
                    "source_group_code": metadata.source_group_code,
                    "stage": metadata.stage,
                    "target_cutoff": snapshot.cutoff_at,
                },
            ).mappings()
        )
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            mapped_row = cast(Mapping[str, Any], row)
            match_id = str(mapped_row["match_id"])
            grouped.setdefault(match_id, []).append(mapped_row)

        target_order = (snapshot.cutoff_at, snapshot.target_match_id)
        timeline: list[EloMatch] = []
        for match_rows in grouped.values():
            first_row = match_rows[0]
            prediction_cutoff_at = cast(datetime, first_row["prediction_cutoff_at"])
            match_id = str(first_row["match_id"])
            if (prediction_cutoff_at, match_id) > target_order:
                continue
            mapping_version = cls._franchise_mapping_version(first_row)
            result_revisions: list[EloResultRevision] = []
            for result_row in match_rows:
                revision_id = result_row["result_revision_id"]
                if revision_id is None:
                    continue
                observed_at = cast(datetime, result_row["result_observed_at"])
                finality_value = str(result_row["result_finality"])
                if finality_value == "corrected":
                    finality_value = ResultFinality.FINAL.value
                result_revisions.append(
                    EloResultRevision(
                        match_id=match_id,
                        revision_id=str(revision_id),
                        revision=int(result_row["result_revision"]),
                        observed_at=observed_at,
                        finalized_at=observed_at,
                        raw_snapshot_sha256=str(result_row["result_raw_sha256"]),
                        finality=ResultFinality(finality_value),
                        home_sets=int(result_row["home_sets"]),
                        away_sets=int(result_row["away_sets"]),
                    )
                )
            timeline.append(
                EloMatch(
                    match_id=match_id,
                    schedule_revision_id=str(first_row["schedule_revision_id"]),
                    schedule_revision=int(first_row["schedule_revision"]),
                    schedule_observed_at=cast(datetime, first_row["schedule_observed_at"]),
                    schedule_raw_snapshot_sha256=str(first_row["schedule_raw_sha256"]),
                    prediction_cutoff_at=prediction_cutoff_at,
                    scheduled_start_at=cast(datetime, first_row["scheduled_start_at"]),
                    season_id=str(first_row["season_id"]),
                    competition=metadata.competition,
                    stage=metadata.stage,
                    division=metadata.division,
                    home_team_id=str(first_row["home_team_id"]),
                    away_team_id=str(first_row["away_team_id"]),
                    availability_policy=snapshot.availability_policy,
                    timing_eligibility=TimingEligibility.ON_TIME,
                    result_finality_policy=ResultFinalityPolicy.FINAL_ONLY,
                    franchise_mapping_version=mapping_version,
                    result_revisions=tuple(result_revisions),
                    home_franchise=FranchiseIdentity(
                        str(first_row["home_franchise_id"]),
                        mapping_version,
                        str(first_row["home_evidence"]),
                    ),
                    away_franchise=FranchiseIdentity(
                        str(first_row["away_franchise_id"]),
                        mapping_version,
                        str(first_row["away_evidence"]),
                    ),
                )
            )
        target = next(
            (match for match in timeline if match.match_id == snapshot.target_match_id),
            None,
        )
        if target is None:
            raise ValueError("target match is missing from the Elo timeline")
        if target.schedule_revision_id != snapshot.schedule_revision_id:
            raise ValueError("target Elo schedule differs from the frozen snapshot")
        return tuple(sorted(timeline, key=lambda match: match.order_key))

    @staticmethod
    def _franchise_mapping_version(row: Mapping[str, Any]) -> str:
        required = (
            "home_franchise_id",
            "home_mapping_version",
            "home_evidence",
            "away_franchise_id",
            "away_mapping_version",
            "away_evidence",
        )
        if any(row[field] is None or not str(row[field]).strip() for field in required):
            raise ValueError("verified franchise identities are required for Elo replay")
        home_version = str(row["home_mapping_version"])
        away_version = str(row["away_mapping_version"])
        if home_version != away_version:
            raise ValueError("home and away franchise mappings must use one version")
        return home_version

    @staticmethod
    def _metadata(
        connection: Connection,
        snapshot: FeatureSnapshot,
    ) -> StatisticalMatchMetadata:
        row = (
            connection.execute(
                text(
                    """
                    SELECT m.source, m.source_group_code, m.season_id,
                           c.source_competition_code, c.stage, c.division,
                           mr.revision AS schedule_revision,
                           mr.observed_at AS schedule_observed_at,
                           mr.scheduled_start_at,
                           raw.sha256 AS schedule_raw_sha256
                    FROM mirror.matches m
                    JOIN mirror.competitions c ON c.id = m.competition_id
                    JOIN mirror.match_revisions mr
                      ON mr.match_id = m.id AND mr.id = :schedule_revision_id
                    JOIN mirror.raw_snapshots raw ON raw.id = mr.raw_snapshot_id
                    WHERE m.id = :match_id
                    """
                ),
                {
                    "match_id": UUID(snapshot.target_match_id),
                    "schedule_revision_id": UUID(snapshot.schedule_revision_id),
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise TerminalJobError("statistical_match_metadata_not_found")
        return StatisticalMatchMetadata(
            source=str(row["source"]),
            source_group_code=str(row["source_group_code"]),
            season_id=str(row["season_id"]),
            competition=str(row["source_competition_code"]),
            stage=str(row["stage"]),
            division=Division(str(row["division"])),
            scheduled_start_at=cast(datetime, row["scheduled_start_at"]),
            schedule_observed_at=cast(datetime, row["schedule_observed_at"]),
            schedule_revision=int(row["schedule_revision"]),
            schedule_raw_snapshot_sha256=str(row["schedule_raw_sha256"]),
        )

    @staticmethod
    def _fifth_set_history(
        connection: Connection,
        snapshot: FeatureSnapshot,
        *,
        eligible_result_ids: tuple[str, ...],
    ) -> tuple[int, int, tuple[str, ...]]:
        accepted_by_features = set(snapshot.lineage.result_revision_ids)
        result_ids = tuple(
            UUID(value) for value in eligible_result_ids if value in accepted_by_features
        )
        if not result_ids:
            return 0, 0, ()
        statement = text(
            """
            SELECT rr.id, rr.home_sets, rr.observed_at,
                   count(ms.id) AS set_count
            FROM mirror.result_revisions rr
            JOIN mirror.match_sets ms ON ms.result_revision_id = rr.id
            WHERE rr.id IN :result_ids
              AND rr.finality IN ('final', 'corrected')
              AND rr.observed_at <= :cutoff_at
            GROUP BY rr.id, rr.home_sets, rr.observed_at
            ORDER BY rr.observed_at, rr.id
            """
        ).bindparams(bindparam("result_ids", expanding=True))
        rows = tuple(
            connection.execute(
                statement,
                {"result_ids": result_ids, "cutoff_at": snapshot.cutoff_at},
            ).mappings()
        )
        fifth = tuple(row for row in rows if int(row["set_count"]) == 5)
        return (
            sum(int(row["home_sets"]) == 3 for row in fifth),
            len(fifth),
            tuple(str(row["id"]) for row in rows),
        )

    @staticmethod
    def _verified_rule(
        connection: Connection,
        snapshot: FeatureSnapshot,
        metadata: StatisticalMatchMetadata,
        *,
        eligible_result_ids: tuple[str, ...],
    ) -> VerifiedSeasonScoreRule | None:
        if not eligible_result_ids:
            return None
        statement = text(
            """
            SELECT DISTINCT rr.rule_version
            FROM mirror.result_revisions rr
            JOIN mirror.matches m ON m.id = rr.match_id
            WHERE rr.id IN :result_ids
              AND m.season_id = :season_id
              AND rr.finality IN ('final', 'corrected')
              AND rr.observed_at <= :cutoff_at
            ORDER BY rr.rule_version
            """
        ).bindparams(bindparam("result_ids", expanding=True))
        rows = connection.execute(
            statement,
            {
                "result_ids": tuple(UUID(value) for value in eligible_result_ids),
                "season_id": UUID(metadata.season_id),
                "cutoff_at": snapshot.cutoff_at,
            },
        ).scalars()
        versions = tuple(str(value) for value in rows)
        if versions != (DEFAULT_SCORE_RULE_VERSION,):
            return None
        evidence = {
            "division": metadata.division.value,
            "rule_version": versions[0],
            "season_id": metadata.season_id,
        }
        return VerifiedSeasonScoreRule(
            season_id=metadata.season_id,
            division=metadata.division,
            rule_version=versions[0],
            mirror_rule_artifact_id=sha256_bytes(canonical_json_bytes(evidence)),
            verified_at=snapshot.cutoff_at,
        )


def ensure_statistical_binding(
    engine: Engine,
) -> tuple[StatisticalBinding, DatabaseStatisticalPredictionRunner]:
    variant_id = uuid5(NAMESPACE_URL, _STATISTICAL_VARIANT_IDENTITY)
    config = {
        "distribution_version": STATISTICAL_DISTRIBUTION_VERSION,
        "elo_config": _ELO_CONFIG.to_dict(),
        "elo_config_id": _ELO_CONFIG.config_id,
        "variant_key": STATISTICAL_VARIANT_KEY,
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO engine.model_variants (
                    id, provider, requested_model, pinned_model_version,
                    prompt_version, prompt_hash, feature_version,
                    output_schema_version, hyperparameters, experiment_group
                ) VALUES (
                    :id, 'statistical', :model, :model,
                    'not-applicable', :prompt_hash, 'feature-v1',
                    'prediction-v1', CAST(:hyperparameters AS jsonb), 'production-baseline'
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": variant_id,
                "model": STATISTICAL_MODEL_VERSION,
                "prompt_hash": "0" * 64,
                "hyperparameters": json.dumps(config, sort_keys=True, separators=(",", ":")),
            },
        )
        stored = (
            connection.execute(
                text(
                    """
                SELECT provider, requested_model, pinned_model_version,
                       feature_version, output_schema_version, hyperparameters
                FROM engine.model_variants WHERE id = :id
                """
                ),
                {"id": variant_id},
            )
            .mappings()
            .one()
        )
    if (
        str(stored["provider"]) != "statistical"
        or str(stored["requested_model"]) != STATISTICAL_MODEL_VERSION
        or str(stored["pinned_model_version"]) != STATISTICAL_MODEL_VERSION
        or str(stored["feature_version"]) != "feature-v1"
        or str(stored["output_schema_version"]) != "prediction-v1"
        or cast(Mapping[str, Any], stored["hyperparameters"]).get("variant_key")
        != STATISTICAL_VARIANT_KEY
        or cast(Mapping[str, Any], stored["hyperparameters"]).get("elo_config_id")
        != _ELO_CONFIG.config_id
    ):
        raise OperationalConfigError("statistical model variant registry row is incompatible")
    runner = DatabaseStatisticalPredictionRunner(
        engine,
        variant_id=variant_id,
        variant_key=STATISTICAL_VARIANT_KEY,
    )
    return StatisticalBinding(variant_id, runner), runner


def provider_variant_identity_matches(
    stored: Mapping[str, object],
    expected: Mapping[str, object],
) -> bool:
    """Return whether an existing deterministic registry row has the exact identity."""

    identity_fields = (
        "provider",
        "requested_model",
        "pinned_model_version",
        "prompt_version",
        "prompt_hash",
        "feature_version",
        "output_schema_version",
        "experiment_group",
    )
    if any(stored.get(field) != expected.get(field) for field in identity_fields):
        return False
    stored_hyperparameters = stored.get("hyperparameters")
    expected_hyperparameters = expected.get("hyperparameters")
    return (
        isinstance(stored_hyperparameters, Mapping)
        and isinstance(expected_hyperparameters, Mapping)
        and dict(stored_hyperparameters) == dict(expected_hyperparameters)
    )


def ensure_provider_bindings(
    engine: Engine,
    live_plan: LiveProviderPlan,
    providers: tuple[PredictionProvider, ...],
) -> dict[str, ProviderBinding]:
    by_key = {provider.variant.variant_id: provider for provider in providers}
    bindings: dict[str, ProviderBinding] = {}
    for item in live_plan.providers:
        key = item.variant.variant_id
        provider = by_key.get(key)
        if provider is None:
            raise OperationalConfigError(f"live provider factory omitted {key}")
        variant_id = uuid5(NAMESPACE_URL, f"https://vlytics.local/variants/{key}")
        variant = item.variant
        hyperparameters = {
            "distribution_version": variant.distribution_version,
            "max_input_tokens": variant.max_input_tokens,
            "max_output_tokens": variant.max_output_tokens,
            "variant_key": key,
            "version_policy": variant.version_policy.value,
        }
        expected_identity: dict[str, object] = {
            "provider": variant.provider.value,
            "requested_model": variant.requested_model_id,
            "pinned_model_version": variant.pinned_model_version,
            "prompt_version": variant.prompt_version,
            "prompt_hash": variant.prompt_hash,
            "feature_version": "feature-v1",
            "output_schema_version": "prediction-v1",
            "hyperparameters": hyperparameters,
            "experiment_group": "production-live",
        }
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO engine.model_variants (
                        id, provider, requested_model, pinned_model_version,
                        prompt_version, prompt_hash, feature_version,
                        output_schema_version, hyperparameters, experiment_group
                    ) VALUES (
                        :id, :provider, :requested_model, :pinned_model_version,
                        :prompt_version, :prompt_hash, 'feature-v1',
                        'prediction-v1', CAST(:hyperparameters AS jsonb), 'production-live'
                    )
                    ON CONFLICT (id) DO NOTHING
                    """
                ),
                {
                    "id": variant_id,
                    "provider": variant.provider.value,
                    "requested_model": variant.requested_model_id,
                    "pinned_model_version": variant.pinned_model_version,
                    "prompt_version": variant.prompt_version,
                    "prompt_hash": variant.prompt_hash,
                    "hyperparameters": json.dumps(
                        hyperparameters, sort_keys=True, separators=(",", ":")
                    ),
                },
            )
            stored_identity = (
                connection.execute(
                    text(
                        """
                        SELECT provider, requested_model, pinned_model_version,
                               prompt_version, prompt_hash, feature_version,
                               output_schema_version, hyperparameters, experiment_group
                        FROM engine.model_variants
                        WHERE id = :id
                        """
                    ),
                    {"id": variant_id},
                )
                .mappings()
                .one()
            )
            if not provider_variant_identity_matches(dict(stored_identity), expected_identity):
                raise OperationalConfigError(
                    f"provider model variant registry row is incompatible for {key}"
                )
        bindings[key] = ProviderBinding(variant_id, provider)
    return bindings


def prediction_schema() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[4] / "contracts" / "prediction-v1.schema.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise OperationalConfigError("prediction-v1 schema must be an object")
    return cast(dict[str, Any], document)


def source_collection_enabled(values: Mapping[str, object]) -> bool:
    source = values.get("source")
    return isinstance(source, Mapping) and source.get("bulk_collection_enabled") is True


def local_budget() -> Decimal:
    """Return a zero-cap fallback; live providers always use durable PostgreSQL limits."""

    return Decimal("0")
