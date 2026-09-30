"""Desktop compilation preserves activation gates and private Provider routing."""

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from vlytics.config import Settings, load_operational_config
from vlytics.engine.providers import build_live_provider_plan, load_variant_registry
from vlytics.storage.migrations import DatabaseRolePasswords

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "desktop_compiler", ROOT / "infra/scripts/compile_desktop_settings.py"
)
assert SPEC and SPEC.loader
COMPILER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMPILER)


def settings(tmp_path, provider="openai"):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "RuntimeRoot": str(ROOT),
                "Provider": provider,
                "ModelId": "synthetic-model",
                "Images": dict.fromkeys(COMPILER.IMAGE_KEYS, "example/image@sha256:" + "a" * 64),
            }
        )
    )
    return path


@pytest.mark.parametrize("provider", ["openai", "anthropic", "google"])
def test_provider_selection_preserves_activation_and_isolation(tmp_path, provider):
    destination = tmp_path / "compiled"
    COMPILER.compile_settings(settings(tmp_path, provider), destination)
    config = tomllib.loads((destination / "operational.toml").read_text("utf-8"))
    registry = tomllib.loads((destination / "variants.toml").read_text("utf-8"))
    assert config["deployment"]["profile"] == "personal_home"
    assert config["source"]["bulk_collection_enabled"] is False
    assert "pricing_to_budget_rate" not in config["ai"]
    assert config["ai"][provider]["version_policy"] == "unconfigured"
    assert [v["provider"] for v in registry["variants"] if v["enabled"]] == [provider]
    assert not next(v for v in registry["variants"] if v["enabled"])["op003_resolved"]
    compose = (destination / "compose.yaml").read_text("utf-8")
    assert f"{provider}_egress:" in compose
    assert f"VLYTICS_{provider.upper()}_PROXY_URL: http://{provider}_egress:8081" in compose
    assert "gw_priority: 1" in compose
    assert "internal: true" in compose
    assert "127.0.0.1:${WEB_PORT:-8080}:8080" in compose
    for other in COMPILER.PROVIDERS - {provider}:
        assert f"{other}_egress:" not in compose
        assert f"VLYTICS_{other.upper()}_API_KEY:" not in compose
    public_environment = (destination / "runtime.env").read_text("utf-8")
    assert "API_KEY=" not in public_environment
    assert "PASSWORD=" not in public_environment
    assert "DATABASE_URL=" not in public_environment
    assert not (destination / "evidence.json").exists()


def test_supplied_evidence_is_copied_exactly_and_changes_require_reapply(tmp_path):
    evidence = tmp_path / "evidence.json"
    evidence.write_bytes(b'{"synthetic":"preserve exact bytes"}\n')
    path = settings(tmp_path)
    data = json.loads(path.read_text())
    data["DryRunEvidencePath"] = str(evidence)
    path.write_text(json.dumps(data))
    destination = tmp_path / "compiled"
    result = COMPILER.compile_settings(path, destination)
    assert (destination / "evidence.json").read_bytes() == evidence.read_bytes()
    original = result["settingsSha256"]
    data["MonthlyBudget"] = 9000
    path.write_text(json.dumps(data))
    changed = COMPILER.compile_settings(path, tmp_path / "changed")
    assert changed["settingsSha256"] != original


@pytest.mark.parametrize(
    "invalid", ["mutable:latest", "x\nPASSWORD=bad", "example@sha256:" + "z" * 64]
)
def test_image_injection_and_mutable_images_are_rejected(tmp_path, invalid):
    path = settings(tmp_path)
    data = json.loads(path.read_text())
    data["Images"]["VLYTICS_BACKEND_IMAGE"] = invalid
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        COMPILER.compile_settings(path, tmp_path / "compiled")


@pytest.mark.parametrize("provider", ["openai", "anthropic", "google"])
def test_reviewed_desktop_settings_match_real_worker_provider_contract(tmp_path, provider):
    path = settings(tmp_path, provider)
    data = json.loads(path.read_text())
    data.update(
        ModelVerified=True,
        PinnedModelVersion="synthetic-model",
        PricingToBudgetRate=1400,
        InputPrice=1,
        OutputPrice=2,
    )
    path.write_text(json.dumps(data))
    destination = tmp_path / "compiled"
    COMPILER.compile_settings(path, destination)
    environ = {
        "VLYTICS_DATABASE_URL": "postgresql://engine@localhost/synthetic",
        f"VLYTICS_{provider.upper()}_API_KEY": "synthetic-key-never-sent",
    }
    config = load_operational_config(
        Settings(
            operational_config_path=destination / "operational.toml",
            operational_schema_path=ROOT / "contracts/config.schema.json",
        ),
        environ=environ,
        component="worker",
    )
    plan = build_live_provider_plan(
        config, load_variant_registry(destination / "variants.toml"), environ=environ
    )
    assert [item.variant.provider.value for item in plan.providers] == [provider]
    assert plan.pricing_to_budget_rate == 1400
    assert plan.providers[0].monthly_budget == 10000


def test_desktop_database_urls_match_migration_login_roles_and_driver():
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed")
    helper = str(ROOT / "infra/scripts/DesktopEnvironment.ps1").replace("'", "''")
    program = f"""
. '{helper}'
$roles = @('MIGRATOR','COLLECTOR','ENGINE','MARKET_INGEST','READ_API')
$urls = @{{}}
foreach ($role in $roles) {{
    $urls[$role] = Get-DesktopRoleDatabaseUrl -Role $role -Password 'synthetic:@/%?'
}}
$urls | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", program], capture_output=True, text=True, check=True
    )
    urls = json.loads(result.stdout)
    logins = dict(DatabaseRolePasswords("collector", "engine", "market", "read").items())
    for role, text in urls.items():
        url = make_url(text)
        assert url.drivername == "postgresql+psycopg"
        assert url.host == "postgres"
        assert url.password == "synthetic:@/%?"
        if role == "MIGRATOR":
            assert url.username == "vlytics_migrator"
        else:
            assert url.username in logins


@pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop operation script")
@pytest.mark.parametrize("preflight_passes", [True, False])
def test_desktop_start_runs_no_docker_mutation_until_preflight_passes(tmp_path, preflight_passes):
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed")
    scripts = tmp_path / "runtime/infra/scripts"
    scripts.mkdir(parents=True)
    for filename in ("Invoke-DesktopOperation.ps1", "DesktopEnvironment.ps1"):
        shutil.copyfile(ROOT / "infra/scripts" / filename, scripts / filename)
    (scripts / "Invoke-Preflight.ps1").write_text(
        "param($EnvironmentFile,$OperationalConfig,$DryRunEvidence,$ComposeFile,$Profile,[switch]$UseProcessSecrets)\n"
        + ("$null = $UseProcessSecrets\n" if preflight_passes else "throw 'Synthetic rejection'\n")
    )
    data = tmp_path / "data"
    generation = data / "deployments/synthetic"
    generation.mkdir(parents=True)
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "RuntimeRoot": str(tmp_path / "runtime"),
                "Provider": "openai",
                "ModelVerified": True,
                "PinnedModelVersion": "synthetic-model",
                "PricingToBudgetRate": 1400,
                "InputPrice": 1,
                "OutputPrice": 2,
            }
        )
    )
    (generation / "manifest.json").write_text(
        json.dumps({"settingsSha256": hashlib.sha256(settings_file.read_bytes()).hexdigest()})
    )
    (generation / "evidence.json").write_text("{}")
    (data / "applied-deployment.txt").write_text(str(generation))

    def quote(path):
        return "'" + str(path).replace("'", "''") + "'"

    program = f"""
$global:Calls = New-Object 'System.Collections.Generic.List[string]'
function docker {{ $global:Calls.Add(($args -join ' ')); $global:LASTEXITCODE = 0 }}
function uv {{ $global:LASTEXITCODE = 0 }}
foreach ($role in @('MIGRATOR','COLLECTOR','ENGINE','MARKET_INGEST','READ_API')) {{
    [Environment]::SetEnvironmentVariable(
        $role + '_DATABASE_PASSWORD', 'synthetic-password', 'Process')
}}
$env:VLYTICS_OPENAI_API_KEY = 'synthetic-key-never-sent'
& {quote(scripts / "Invoke-DesktopOperation.ps1")} -Action Start `
    -SettingsFile {quote(settings_file)} -DataDirectory {quote(data)}
ConvertTo-Json -InputObject @($global:Calls) -Compress
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", program], capture_output=True, check=True
    )
    commands = json.loads(result.stdout.decode("utf-8").splitlines()[-1])
    if not preflight_passes:
        assert commands == []
    else:
        assert len(commands) == 4, result.stderr.decode("utf-8")
        assert commands[0].endswith("stop worker api")
        assert commands[1].endswith("up -d --wait postgres")
        assert commands[2].endswith("run --rm migrate")
        assert commands[3].endswith("up -d --wait --remove-orphans")
    assert "synthetic-password" not in result.stdout.decode("utf-8")
    assert "synthetic-key-never-sent" not in result.stdout.decode("utf-8")
