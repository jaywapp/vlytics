"""Compile non-secret desktop settings into an immutable home deployment preview."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tomllib
from pathlib import Path

PROVIDERS = {"openai", "anthropic", "google"}
IMAGE_KEYS = (
    "VLYTICS_BACKEND_IMAGE",
    "VLYTICS_FRONTEND_IMAGE",
    "VLYTICS_NODE_BUILD_IMAGE",
    "VLYTICS_NGINX_RUNTIME_IMAGE",
    "VLYTICS_POSTGRES_IMAGE",
    "VLYTICS_PYTHON_BUILD_IMAGE",
    "VLYTICS_UV_BUILD_IMAGE",
)


def scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(scalar(item) for item in value) + "]"
    raise ValueError("Unsupported configuration value")


def dump_toml(document: dict) -> str:
    lines: list[str] = []

    def table(values: dict, prefix: str = "") -> None:
        for key, value in values.items():
            if not isinstance(value, dict) and not (
                isinstance(value, list) and value and isinstance(value[0], dict)
            ):
                lines.append(f"{key} = {scalar(value)}")
        for key, value in values.items():
            name = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                lines.extend(["", f"[{name}]"])
                table(value, name)
            elif isinstance(value, list) and value and isinstance(value[0], dict):
                for item in value:
                    lines.extend(["", f"[[{name}]]"])
                    table(item, name)

    table(document)
    return "\n".join(lines) + "\n"


def compile_settings(settings_file: Path, destination: Path) -> dict:
    raw = settings_file.read_bytes()
    settings = {key.lower(): value for key, value in json.loads(raw).items()}

    def get(name: str, default=None):
        return settings.get(name.lower(), default)

    root = Path(get("RuntimeRoot")).resolve(strict=True)
    provider = get("Provider", "openai")
    if provider not in PROVIDERS:
        raise ValueError("Unknown Provider")
    port = get("WebPort", 8080)
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("Invalid web port")
    images = get("Images", {})
    if set(images) != set(IMAGE_KEYS):
        raise ValueError("All seven image fields are required")
    for value in images.values():
        if not isinstance(value, str) or any(c in value for c in "\r\n\x00"):
            raise ValueError("Invalid image value")
        if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:\-]*@sha256:[a-f0-9]{64}", value):
            raise ValueError("Image must be pinned by SHA-256")
    config = tomllib.loads((root / "infra/operational.home.example.toml").read_text("utf-8"))
    registry = tomllib.loads((root / "config/variants.home.toml").read_text("utf-8"))
    verified = bool(get("ModelVerified", False))
    pinned = get("PinnedModelVersion", "") or "__REQUIRED_AFTER_MODEL_SMOKE__"
    model = get("ModelId", "") or "__UNRESOLVED_OP_003__"
    limits = {
        "daily_budget_amount": get("DailyBudget", 1000),
        "monthly_budget_amount": get("MonthlyBudget", 10000),
        "max_calls_per_match": get("MaxCallsPerMatch", 3),
        "daily_call_limit": get("DailyCallLimit", 100),
        "monthly_call_limit": get("MonthlyCallLimit", 1000),
        "max_input_tokens_per_call": get("MaxInputTokens", 4000),
        "max_output_tokens_per_call": get("MaxOutputTokens", 1000),
    }
    for value in limits.values():
        if not isinstance(value, int | float) or value <= 0:
            raise ValueError("Budget and call limits must be positive")
    for name in PROVIDERS:
        config["ai"][name]["enabled"] = name == provider
        if name == provider:
            config["ai"][name].update(limits)
            config["ai"][name].update(
                model_id=model,
                pinned_model_version=pinned,
                version_policy="verify_resolved_model_id" if verified else "unconfigured",
            )
    rate = get("PricingToBudgetRate", 0)
    if rate > 0:
        config["ai"]["pricing_to_budget_rate"] = rate
    registry["budget"].update(
        daily_amount=str(get("DailyBudget", 1000)), monthly_amount=str(get("MonthlyBudget", 10000))
    )
    for variant in registry["variants"]:
        enabled = variant["provider"] == provider
        variant["enabled"] = enabled
        if enabled:
            variant.update(
                op003_resolved=verified,
                requested_model_id=model,
                pinned_model_version=pinned,
                version_policy="verify_resolved_model_id" if verified else "unconfigured",
                max_input_tokens=get("MaxInputTokens", 4000),
                max_output_tokens=get("MaxOutputTokens", 1000),
                input_cost_per_million=str(get("InputPrice", 0)),
                output_cost_per_million=str(get("OutputPrice", 0)),
                pricing_currency="USD",
            )
    destination.mkdir(parents=True, exist_ok=False)
    registry_text = dump_toml(registry)
    (destination / "variants.toml").write_text(registry_text, encoding="utf-8", newline="\n")
    config["activation"]["provider_registry_sha256"] = hashlib.sha256(
        registry_text.encode("utf-8")
    ).hexdigest()
    (destination / "operational.toml").write_text(dump_toml(config), encoding="utf-8", newline="\n")
    shutil.copyfile(root / "config/source.toml", destination / "source.toml")
    evidence = get("DryRunEvidencePath", "")
    if evidence and Path(evidence).is_file():
        shutil.copyfile(evidence, destination / "evidence.json")
    compose = (root / "infra/compose.home.yaml").read_text("utf-8")
    compose = compose.replace("openai", provider).replace("OPENAI", provider.upper())
    # Generated composition keeps all application hardening; make relative mounts explicit.
    for relative in (
        "./postgres-init",
        "./nginx.operator-ingress.conf",
        "../contracts/config.schema.json",
    ):
        absolute = (root / "infra" / relative).resolve().as_posix()
        compose = compose.replace(relative + ":", absolute + ":")
    (destination / "compose.yaml").write_text(compose, encoding="utf-8", newline="\n")
    values = dict(images)
    values.update(
        {
            "WEB_PORT": str(port),
            "POSTGRES_DB": "vlytics",
            "VLYTICS_OPERATIONAL_CONFIG_FILE": (destination / "operational.toml").as_posix(),
            "VLYTICS_PROVIDER_REGISTRY_FILE": (destination / "variants.toml").as_posix(),
            "VLYTICS_SOURCE_REGISTRY_FILE": (destination / "source.toml").as_posix(),
            "VLYTICS_LIVE_DRY_RUN_EVIDENCE_FILE": (destination / "evidence.json").as_posix(),
        }
    )
    (destination / "runtime.env").write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8"
    )
    manifest = {"settingsSha256": hashlib.sha256(raw).hexdigest(), "provider": provider}
    (destination / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("settings", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        compile_settings(args.settings, args.destination)
    except (ValueError, OSError, TypeError, KeyError):
        # Never propagate arbitrary user-controlled settings or file contents to logs.
        raise SystemExit("Desktop configuration compilation failed") from None
