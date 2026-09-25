from copy import deepcopy

from vlytics.ops.runtime import provider_variant_identity_matches


def _identity() -> dict[str, object]:
    return {
        "provider": "openai",
        "requested_model": "model-v1",
        "pinned_model_version": "model-v1-2026-09-01",
        "prompt_version": "prompt-v1",
        "prompt_hash": "a" * 64,
        "feature_version": "feature-v1",
        "output_schema_version": "prediction-v1",
        "hyperparameters": {
            "distribution_version": "distribution-v1",
            "max_input_tokens": 1000,
            "max_output_tokens": 500,
            "variant_key": "openai-v1",
            "version_policy": "immutable_model_id",
        },
        "experiment_group": "production-live",
    }


def test_provider_variant_identity_requires_exact_registry_metadata() -> None:
    expected = _identity()
    assert provider_variant_identity_matches(deepcopy(expected), expected)

    for field in (
        "provider",
        "requested_model",
        "pinned_model_version",
        "prompt_version",
        "prompt_hash",
        "feature_version",
        "output_schema_version",
        "experiment_group",
    ):
        stored = deepcopy(expected)
        stored[field] = "different"
        assert not provider_variant_identity_matches(stored, expected)

    stored = deepcopy(expected)
    hyperparameters = stored["hyperparameters"]
    assert isinstance(hyperparameters, dict)
    hyperparameters["max_input_tokens"] = 2000
    assert not provider_variant_identity_matches(stored, expected)
