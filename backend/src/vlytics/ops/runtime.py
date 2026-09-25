"""Production runtime adapters for the durable worker process."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import Connection, Engine, text

from vlytics.config import OperationalConfigError
from vlytics.engine.features import (
    AsOfFeatureBuilder,
    AvailabilityPolicy,
    FeatureSnapshot,
    FeatureStatus,
    HistoricalMatch,
    MatchScheduleRevision,
    ResultRevision,
    SetScore,
    TargetMatch,
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
    JointScorePredictor,
    PointModelConfig,
    SetModelConfig,
    SetOutcomePrediction,
    SetTrainingCohort,
    VerifiedSeasonScoreRule,
    VerifiedSetPredictionArtifact,
    independent_set_distribution,
    solve_regular_set_probability,
)
from vlytics.engine.providers import LiveProviderPlan, PredictionProvider
from vlytics.engine.providers.models import canonical_json_bytes, sha256_bytes
from vlytics.ops.scheduler import JobHandler, TerminalJobError
from vlytics.storage.migrations import load_migrations

STATISTICAL_VARIANT_KEY = "statistical-joint-v1"
STATISTICAL_MODEL_VERSION = "feature-form-p5-joint-v1"
STATISTICAL_DISTRIBUTION_VERSION = "joint-score-v1"
_FRANCHISE_MAPPING_VERSION = "season-team-isolated-v1"


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
    """Build leakage-safe feature-v1 snapshots from accepted mirror revisions."""

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
        with self._engine.connect() as connection:
            target = self._target(connection, schedule)
            history = self._history(connection, target, cutoff_at=cutoff_at)
        try:
            return self._builder.build(
                target=target,
                cutoff_at=cutoff_at,
                captured_at=captured_at,
                policy=AvailabilityPolicy.LIVE_PROSPECTIVE,
                matches=history,
            )
        except ValueError as error:
            raise TerminalJobError("feature_snapshot_inputs_ineligible") from error

    @staticmethod
    def _target(connection: Connection, schedule: MatchExecutionContext) -> TargetMatch:
        row = (
            connection.execute(
                text(
                    """
                    SELECT m.id, m.season_id, m.competition_id,
                           mr.id AS schedule_revision_id, mr.scheduled_start_at,
                           mr.observed_at, mr.home_team_id, mr.away_team_id
                    FROM mirror.matches m
                    JOIN mirror.match_revisions mr ON mr.match_id = m.id
                    WHERE m.id = :match_id AND mr.id = :schedule_revision_id
                    """
                ),
                {
                    "match_id": schedule.match_id,
                    "schedule_revision_id": schedule.schedule_revision_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise TerminalJobError("feature_target_schedule_not_found")
        return TargetMatch(
            id=str(row["id"]),
            schedule_revision_id=str(row["schedule_revision_id"]),
            season_id=str(row["season_id"]),
            competition_id=str(row["competition_id"]),
            scheduled_start_at=cast(datetime, row["scheduled_start_at"]),
            schedule_observed_at=cast(datetime, row["observed_at"]),
            home_team_id=str(row["home_team_id"]),
            away_team_id=str(row["away_team_id"]),
        )

    @staticmethod
    def _history(
        connection: Connection,
        target: TargetMatch,
        *,
        cutoff_at: datetime,
    ) -> tuple[HistoricalMatch, ...]:
        rows = connection.execute(
            text(
                """
                WITH schedules AS (
                    SELECT DISTINCT ON (mr.match_id)
                           mr.match_id, mr.id, mr.revision, mr.observed_at,
                           mr.scheduled_start_at, mr.home_team_id, mr.away_team_id,
                           raw.sha256 AS raw_sha256
                    FROM mirror.match_revisions mr
                    JOIN mirror.raw_snapshots raw ON raw.id = mr.raw_snapshot_id
                    WHERE mr.observed_at <= :cutoff_at
                    ORDER BY mr.match_id, mr.revision DESC, mr.observed_at DESC
                ),
                results AS (
                    SELECT DISTINCT ON (rr.match_id)
                           rr.match_id, rr.id, rr.revision, rr.observed_at,
                           rr.home_sets, rr.away_sets, rr.finality,
                           raw.sha256 AS raw_sha256
                    FROM mirror.result_revisions rr
                    JOIN mirror.raw_snapshots raw ON raw.id = rr.raw_snapshot_id
                    WHERE rr.observed_at <= :cutoff_at
                    ORDER BY rr.match_id, rr.revision DESC, rr.observed_at DESC
                )
                SELECT m.id, m.season_id, m.competition_id,
                       schedules.id AS schedule_id, schedules.revision AS schedule_revision,
                       schedules.observed_at AS schedule_observed_at,
                       schedules.scheduled_start_at, schedules.home_team_id,
                       schedules.away_team_id, schedules.raw_sha256 AS schedule_sha256,
                       results.id AS result_id, results.revision AS result_revision,
                       results.observed_at AS result_observed_at,
                       results.home_sets, results.away_sets, results.finality,
                       results.raw_sha256 AS result_sha256
                FROM mirror.matches m
                JOIN schedules ON schedules.match_id = m.id
                JOIN results ON results.match_id = m.id
                WHERE m.id <> :target_match_id
                  AND m.season_id = :season_id
                  AND m.competition_id = :competition_id
                  AND schedules.scheduled_start_at < :cutoff_at
                ORDER BY schedules.scheduled_start_at, m.id
                """
            ),
            {
                "cutoff_at": cutoff_at,
                "target_match_id": UUID(target.id),
                "season_id": UUID(target.season_id),
                "competition_id": UUID(target.competition_id),
            },
        ).mappings()
        history: list[HistoricalMatch] = []
        for row in rows:
            result_observed_at = cast(datetime, row["result_observed_at"])
            scheduled_start_at = cast(datetime, row["scheduled_start_at"])
            if result_observed_at < scheduled_start_at:
                continue
            set_rows = connection.execute(
                text(
                    """
                    SELECT home_points, away_points
                    FROM mirror.match_sets
                    WHERE result_revision_id = :result_revision_id
                    ORDER BY set_number
                    """
                ),
                {"result_revision_id": row["result_id"]},
            ).mappings()
            sets = tuple(
                SetScore(int(item["home_points"]), int(item["away_points"])) for item in set_rows
            )
            finality = str(row["finality"])
            if finality == "corrected":
                finality = "final"
            history.append(
                HistoricalMatch(
                    id=str(row["id"]),
                    season_id=str(row["season_id"]),
                    competition_id=str(row["competition_id"]),
                    schedule_revisions=(
                        MatchScheduleRevision(
                            id=str(row["schedule_id"]),
                            revision=int(row["schedule_revision"]),
                            observed_at=cast(datetime, row["schedule_observed_at"]),
                            raw_snapshot_sha256=str(row["schedule_sha256"]),
                            scheduled_start_at=scheduled_start_at,
                            ended_at=result_observed_at,
                            home_team_id=str(row["home_team_id"]),
                            away_team_id=str(row["away_team_id"]),
                        ),
                    ),
                    result_revisions=(
                        ResultRevision(
                            id=str(row["result_id"]),
                            revision=int(row["result_revision"]),
                            observed_at=result_observed_at,
                            raw_snapshot_sha256=str(row["result_sha256"]),
                            home_sets=int(row["home_sets"]),
                            away_sets=int(row["away_sets"]),
                            sets=sets,
                            finality=finality,
                        ),
                    ),
                )
            )
        return tuple(history)


@dataclass(frozen=True)
class StatisticalMatchMetadata:
    season_id: str
    competition: str
    stage: str
    division: Division


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
            fifth_set_home_wins, fifth_set_matches, result_ids = self._fifth_set_history(
                connection,
                snapshot,
            )
            verified_rule = self._verified_rule(connection, snapshot, metadata)

        home_probability = self._home_probability(snapshot)
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
            input_version="feature-form-v1",
            franchise_mapping_version=_FRANCHISE_MAPPING_VERSION,
        )
        set_config = SetModelConfig()
        manifest = {
            "result_revision_ids": list(result_ids),
            "fifth_set_home_wins": fifth_set_home_wins,
            "fifth_set_matches": fifth_set_matches,
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
        upstream_config_id = sha256_bytes(
            canonical_json_bytes(
                {
                    "feature_version": snapshot.feature_version,
                    "formula": "balanced_recent_and_season_win_rate_v1",
                }
            )
        )
        upstream_prediction_id = sha256_bytes(
            canonical_json_bytes(
                {
                    "config_id": upstream_config_id,
                    "home_win_probability": home_probability,
                    "snapshot_id": str(snapshot_id),
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
            source_model_version="feature-form-v1",
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

    @staticmethod
    def _home_probability(snapshot: FeatureSnapshot) -> float:
        def usable(side: str, key: str) -> float | None:
            value = snapshot.team_features[side][key]
            if value.status is FeatureStatus.AVAILABLE and value.value is not None:
                return value.value
            return None

        for key in ("recent_10_match_win_rate", "season_win_rate", "recent_5_match_win_rate"):
            home = usable("home", key)
            away = usable("away", key)
            if home is not None and away is not None:
                return min(0.95, max(0.05, (home + (1.0 - away)) / 2.0))
        return 0.5

    @staticmethod
    def _metadata(
        connection: Connection,
        snapshot: FeatureSnapshot,
    ) -> StatisticalMatchMetadata:
        row = (
            connection.execute(
                text(
                    """
                    SELECT m.season_id, c.source_competition_code,
                           c.stage, c.division
                    FROM mirror.matches m
                    JOIN mirror.competitions c ON c.id = m.competition_id
                    WHERE m.id = :match_id
                    """
                ),
                {"match_id": UUID(snapshot.target_match_id)},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise TerminalJobError("statistical_match_metadata_not_found")
        return StatisticalMatchMetadata(
            season_id=str(row["season_id"]),
            competition=str(row["source_competition_code"]),
            stage=str(row["stage"]),
            division=Division(str(row["division"])),
        )

    @staticmethod
    def _fifth_set_history(
        connection: Connection,
        snapshot: FeatureSnapshot,
    ) -> tuple[int, int, tuple[str, ...]]:
        rows = connection.execute(
            text(
                """
                SELECT rr.id, rr.home_sets,
                       (SELECT count(*) FROM mirror.match_sets ms
                        WHERE ms.result_revision_id = rr.id) AS set_count
                FROM mirror.result_revisions rr
                JOIN mirror.matches m ON m.id = rr.match_id
                JOIN (
                    SELECT match_id, max(revision) AS revision
                    FROM mirror.result_revisions
                    WHERE observed_at <= :cutoff_at
                    GROUP BY match_id
                ) latest ON latest.match_id = rr.match_id AND latest.revision = rr.revision
                WHERE m.season_id = (
                    SELECT season_id FROM mirror.matches WHERE id = :match_id
                )
                  AND m.competition_id = (
                    SELECT competition_id FROM mirror.matches WHERE id = :match_id
                )
                  AND rr.finality IN ('final', 'corrected')
                  AND rr.observed_at <= :cutoff_at
                ORDER BY rr.observed_at, rr.id
                """
            ),
            {"cutoff_at": snapshot.cutoff_at, "match_id": UUID(snapshot.target_match_id)},
        ).mappings()
        all_rows = tuple(rows)
        fifth = tuple(row for row in all_rows if int(row["set_count"]) == 5)
        return (
            sum(int(row["home_sets"]) == 3 for row in fifth),
            len(fifth),
            tuple(str(row["id"]) for row in all_rows),
        )

    @staticmethod
    def _verified_rule(
        connection: Connection,
        snapshot: FeatureSnapshot,
        metadata: StatisticalMatchMetadata,
    ) -> VerifiedSeasonScoreRule | None:
        rows = connection.execute(
            text(
                """
                SELECT DISTINCT rr.rule_version
                FROM mirror.result_revisions rr
                JOIN mirror.matches m ON m.id = rr.match_id
                WHERE m.season_id = :season_id
                  AND rr.finality IN ('final', 'corrected')
                  AND rr.observed_at <= :cutoff_at
                ORDER BY rr.rule_version
                """
            ),
            {"season_id": UUID(metadata.season_id), "cutoff_at": snapshot.cutoff_at},
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
    variant_id = uuid5(NAMESPACE_URL, "https://vlytics.local/variants/statistical-joint-v1")
    config = {
        "distribution_version": STATISTICAL_DISTRIBUTION_VERSION,
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
