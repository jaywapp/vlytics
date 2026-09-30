"""Run the package checker against both checkout newline styles."""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from vlytics.config import Settings, load_operational_config
from vlytics.ops.scheduler import load_live_dry_run_evidence

ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def test_internal_compose_fixture_evidence_matches_worker_configuration(tmp_path):
    smoke = (ROOT / "infra/scripts/ci-production-internal-smoke.sh").read_text(encoding="utf-8")
    config_match = re.search(r'cat > "\$operational_config" <<\'TOML\'\n(.*?)\nTOML', smoke, re.S)
    generator_match = re.search(
        r'python3 - "\$operational_config" "\$dry_run_evidence" <<\'PY\'\n(.*?)\nPY',
        smoke,
        re.S,
    )
    assert config_match is not None
    assert generator_match is not None
    config_path = tmp_path / "operational.toml"
    evidence_path = tmp_path / "evidence.json"
    config_path.write_text(config_match.group(1), encoding="utf-8")
    subprocess.run(
        [sys.executable, "-c", generator_match.group(1), str(config_path), str(evidence_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    config = load_operational_config(
        Settings(
            operational_config_path=config_path,
            operational_schema_path=ROOT / "contracts/config.schema.json",
        ),
        environ={
            "VLYTICS_DATABASE_URL": "postgresql://engine@localhost/synthetic",
            "VLYTICS_OPENAI_API_KEY": "synthetic-openai-never-sent",
            "VLYTICS_ANTHROPIC_API_KEY": "synthetic-anthropic-never-sent",
            "VLYTICS_GOOGLE_API_KEY": "synthetic-google-never-sent",
        },
        component="worker",
    )
    evidence = load_live_dry_run_evidence(config, evidence_path)
    assert set(evidence.providers_verified) == set(config.enabled_providers)


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed")
@pytest.mark.parametrize("profile", ["standard", "personal_home"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["LF", "CRLF"])
def test_package_validator_accepts_checkout_newlines(tmp_path, newline, profile):
    files = [
        "infra/scripts/Test-DeploymentPackage.ps1",
        "infra/scripts/Backup-Database.ps1",
        "infra/scripts/Invoke-RestoreDrill.ps1",
        "infra/scripts/Invoke-Preflight.ps1",
        "infra/scripts/ProviderEgress.ps1",
        "infra/postgres-init/001-create-migrator.sql",
        "infra/compose.production.yaml",
        "infra/.env.example",
        "infra/operational.production.example.toml",
        "frontend/Dockerfile",
        "frontend/nginx.production.conf",
        "infra/nginx.operator-ingress.conf",
        "frontend/.dockerignore",
        "contracts/config.schema.json",
        "infra/compose.home.yaml",
        "infra/.env.home.example",
        "infra/operational.home.example.toml",
    ]
    for relative in files:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(
            (ROOT / relative).read_text(encoding="utf-8-sig").replace("\n", newline).encode("utf-8")
        )
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-File",
            str(tmp_path / "infra/scripts/Test-DeploymentPackage.ps1"),
            "-Profile",
            profile,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed")
def test_preflight_rejects_a_different_compose_mount(tmp_path):
    first = tmp_path / "first.toml"
    second = tmp_path / "second.toml"
    first.write_text("first")
    second.write_text("second")
    script = tmp_path / "check.ps1"
    helper = ROOT / "infra/scripts/DeploymentPaths.ps1"
    script.write_text(
        "param($Helper, $First, $Second, $Base)\n"
        ". $Helper\n"
        "Assert-DeploymentFileBinding $First $First $Base 'test' | Out-Null\n"
        "$rejected = $false\n"
        "try { Assert-DeploymentFileBinding $First $Second $Base 'test' | Out-Null } "
        "catch { $rejected = $true }\n"
        "if (-not $rejected) { throw 'Mismatched config accepted' }\n"
    )
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-File",
            str(script),
            str(helper),
            str(first),
            str(second),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed")
def test_home_package_rejects_standard_config():
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-File",
            str(ROOT / "infra/scripts/Test-DeploymentPackage.ps1"),
            "-Profile",
            "personal_home",
            "-OperationalConfig",
            str(ROOT / "infra/operational.production.example.toml"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode != 0
    assert "Package profile must match" in result.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed")
def test_home_preflight_requires_isolated_reviewed_egress(tmp_path):
    script = tmp_path / "egress-guard.ps1"
    script.write_text(
        "param($Preflight)\n"
        "Set-StrictMode -Version 2.0\n"
        "$tokens = $null; $parseErrors = $null\n"
        "$ast = [System.Management.Automation.Language.Parser]::ParseFile("
        "$Preflight, [ref]$tokens, [ref]$parseErrors)\n"
        "if ($parseErrors.Count -ne 0) { throw 'Preflight parse failure' }\n"
        "$function = $ast.Find({ param($node) "
        "$node -is [System.Management.Automation.Language.FunctionDefinitionAst] "
        "-and $node.Name -eq 'Assert-HomeProviderEgress' }, $true)\n"
        "Invoke-Expression $function.Extent.Text\n"
        "$json = @'\n"
        '{"services":{"worker":{"image":"test-backend",'
        '"environment":{"VLYTICS_OPENAI_PROXY_URL":'
        '"http://openai_egress:8081","VLYTICS_OPENAI_API_KEY":"synthetic-key"},'
        '"networks":{"private":{}},'
        '"depends_on":{"openai_egress":{"condition":"service_healthy"}}},'
        '"openai_egress":{"image":"test-backend",'
        '"command":["python","-m","vlytics.ops.provider_egress"],'
        '"environment":{"VLYTICS_EGRESS_BIND":"0.0.0.0:8081",'
        '"VLYTICS_EGRESS_PROVIDER":"openai"},'
        '"networks":{"private":{},"openai_access":{"gw_priority":1}},'
        '"healthcheck":{"test":["CMD","python","-c","pass"]}}},'
        '"networks":{"private":{"internal":true},"openai_access":{"driver":"bridge"}}}\n'
        "'@\n"
        "Assert-HomeProviderEgress ($json | ConvertFrom-Json)\n"
        "foreach ($case in @('missing', 'proxy', 'worker_network', 'port', 'wrong_provider', "
        "'relay_secret', 'disabled_provider', 'missing_key', 'private_egress', "
        "'dependency', 'health', 'mount', 'gateway', 'private_gateway')) {\n"
        "  $rendered = $json | ConvertFrom-Json\n"
        "  switch ($case) {\n"
        "    'missing' { $rendered.services.PSObject.Properties.Remove('openai_egress') }\n"
        "    'proxy' { $rendered.services.worker.environment.VLYTICS_OPENAI_PROXY_URL = "
        "'http://unreviewed:8081' }\n"
        "    'worker_network' { $rendered.services.worker.networks | "
        "Add-Member NoteProperty openai_access ([pscustomobject]@{}) }\n"
        "    'port' { $rendered.services.openai_egress | "
        "Add-Member NoteProperty ports @('8081:8081') }\n"
        "    'wrong_provider' { $rendered.services.openai_egress.environment."
        "VLYTICS_EGRESS_PROVIDER = 'anthropic' }\n"
        "    'relay_secret' { $rendered.services.openai_egress.environment | "
        "Add-Member NoteProperty VLYTICS_OPENAI_API_KEY 'synthetic-key' }\n"
        "    'disabled_provider' { $rendered.services.worker.environment | "
        "Add-Member NoteProperty VLYTICS_GOOGLE_PROXY_URL 'http://google_egress:8081' }\n"
        "    'missing_key' { $rendered.services.worker.environment.PSObject.Properties."
        "Remove('VLYTICS_OPENAI_API_KEY') }\n"
        "    'private_egress' { $rendered.networks.private.internal = $false }\n"
        "    'dependency' { $rendered.services.worker.depends_on.openai_egress.condition = "
        "'service_started' }\n"
        "    'health' { $rendered.services.openai_egress.healthcheck | "
        "Add-Member NoteProperty disable $true }\n"
        "    'mount' { $rendered.services.openai_egress | "
        "Add-Member NoteProperty volumes @('/private:/run/private:ro') }\n"
        "    'gateway' {\n"
        "      $rendered.services.openai_egress.networks.openai_access.gw_priority = 0\n"
        "    }\n"
        "    'private_gateway' { $rendered.services.openai_egress.networks.private | "
        "Add-Member NoteProperty gw_priority 2 }\n"
        "  }\n"
        "  $rejected = $false\n"
        "  try { Assert-HomeProviderEgress $rendered } catch { $rejected = $true }\n"
        "  if (-not $rejected) { throw ('Unsafe egress accepted: ' + $case) }\n"
        "}\n"
        "$multi = $json | ConvertFrom-Json\n"
        "$other = ($json | ConvertFrom-Json).services.openai_egress\n"
        "$other.environment.VLYTICS_EGRESS_PROVIDER = 'anthropic'\n"
        "$other.networks = [pscustomobject]@{ private = [pscustomobject]@{}; "
        "anthropic_access = [pscustomobject]@{ gw_priority = 1 } }\n"
        "$multi.services | Add-Member NoteProperty anthropic_egress $other\n"
        "$multi.services.worker.depends_on | Add-Member NoteProperty anthropic_egress "
        "([pscustomobject]@{ condition = 'service_healthy' })\n"
        "$multi.networks | Add-Member NoteProperty anthropic_access "
        "([pscustomobject]@{ driver = 'bridge' })\n"
        "$multi.services.worker.environment | Add-Member NoteProperty "
        "VLYTICS_ANTHROPIC_PROXY_URL 'http://anthropic_egress:8081'\n"
        "$multi.services.worker.environment | Add-Member NoteProperty "
        "VLYTICS_ANTHROPIC_API_KEY 'synthetic-key-two'\n"
        "Assert-HomeProviderEgress $multi @('openai', 'anthropic')\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-File",
            str(script),
            str(ROOT / "infra/scripts/ProviderEgress.ps1"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed")
def test_preflight_rejects_equal_role_secrets_and_unsafe_windows_clock(tmp_path):
    script = tmp_path / "preflight-guards.ps1"
    script.write_text(
        "param($Preflight)\n"
        "$tokens = $null\n"
        "$parseErrors = $null\n"
        "$ast = [System.Management.Automation.Language.Parser]::ParseFile("
        "$Preflight, [ref]$tokens, [ref]$parseErrors)\n"
        "if ($parseErrors.Count -ne 0) { throw 'Preflight script has parse errors' }\n"
        "$names = @('Assert-DistinctRoleSecrets', 'Assert-WindowsClockStatus')\n"
        "$functions = $ast.FindAll({ param($node) "
        "$node -is [System.Management.Automation.Language.FunctionDefinitionAst] "
        "-and $names -contains $node.Name }, $true)\n"
        "if ($functions.Count -ne $names.Count) { throw 'Preflight guard functions are missing' }\n"
        "foreach ($function in $functions) { Invoke-Expression $function.Extent.Text }\n"
        "$distinct = @{ VLYTICS_OPERATOR_AUTH_SECRET = 'operator'; "
        "VLYTICS_READONLY_AUTH_SECRET = 'readonly' }\n"
        "Assert-DistinctRoleSecrets $distinct\n"
        "$same = @{ VLYTICS_OPERATOR_AUTH_SECRET = 'shared'; "
        "VLYTICS_READONLY_AUTH_SECRET = 'shared' }\n"
        "$rejected = $false\n"
        "try { Assert-DistinctRoleSecrets $same } catch { $rejected = $true }\n"
        "if (-not $rejected) { throw 'Equal role secrets were accepted' }\n"
        "$healthy = @(\n"
        "  'Localized Field A: 0(no warning)',\n"
        "  'Localized Field B: 3',\n"
        "  'Localized Field C: -23',\n"
        "  'Localized Field D: 0.01s',\n"
        "  'Localized Field E: 0.02s',\n"
        "  'Localized Field F: 0x01020304',\n"
        "  'Localized Field G: 2026-09-28 10:00:00 +09:00',\n"
        "  'Localized Field H: time.example.invalid',\n"
        "  'Localized Field I: 10'\n"
        ")\n"
        "$now = [DateTimeOffset]::Parse('2026-09-28T10:30:00+09:00')\n"
        "Assert-WindowsClockStatus $healthy @('10:30:00, +0.2500000s') $now\n"
        "$badLeap = $healthy.Clone()\n"
        "$badLeap[0] = 'Localized Field A: 3(not synchronized)'\n"
        "$rejected = $false\n"
        "try { Assert-WindowsClockStatus $badLeap @('10:30:00, +0.1s') $now } "
        "catch { $rejected = $true }\n"
        "if (-not $rejected) { throw 'Unsynchronized leap indicator was accepted' }\n"
        "$stale = $healthy.Clone()\n"
        "$stale[6] = 'Localized Field G: 2026-09-28 08:00:00 +09:00'\n"
        "$rejected = $false\n"
        "try { Assert-WindowsClockStatus $stale @('10:30:00, +0.1s') $now } "
        "catch { $rejected = $true }\n"
        "if (-not $rejected) { throw 'Stale synchronization was accepted' }\n"
        "$rejected = $false\n"
        "try { Assert-WindowsClockStatus $healthy @('10:30:00, +1.5000000s') $now } "
        "catch { $rejected = $true }\n"
        "if (-not $rejected) { throw 'Excessive clock offset was accepted' }\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-File",
            str(script),
            str(ROOT / "infra/scripts/Invoke-Preflight.ps1"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed")
def test_postgres_tools_preserve_tls_and_unset_absent_options(tmp_path):
    script = tmp_path / "postgres-options.ps1"
    script.write_text(
        "param($Helper)\n. $Helper\n"
        "Remove-Item Env:PGCHANNELBINDING -ErrorAction SilentlyContinue\n"
        "$previous = Push-PostgresEnvironment 'postgresql://operator@localhost/source'\n"
        "if (Test-Path Env:PGCHANNELBINDING) { throw 'Absent libpq option must not be empty' }\n"
        "Pop-PostgresEnvironment $previous\n"
        "$url = New-DatabaseUrl 'postgresql://operator@localhost/source?sslmode=verify-full"
        "&channel_binding=require' 'restored'\n"
        "if ($url -notmatch '/restored\\?') { throw 'Database was not replaced' }\n"
        "if ($url -notmatch 'sslmode=verify-full' -or "
        "$url -notmatch 'channel_binding=require') { throw 'TLS options lost' }\n"
    )
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-File",
            str(script),
            str(ROOT / "infra/scripts/PostgresTools.ps1"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed")
@pytest.mark.parametrize("invalid_input", ["duplicate_family", "mutable_backend"])
def test_release_wrapper_rejects_incomplete_or_mutable_inventory(tmp_path, invalid_input):
    digest = "a" * 64
    backend = (
        "example/backend:latest"
        if invalid_input == "mutable_backend"
        else f"example/backend@sha256:{digest}"
    )
    families = ["python", "uv", "postgres", "node", "nginx"]
    if invalid_input == "duplicate_family":
        families[-1] = "node"
    bases = ",".join(f"'{family}@sha256:{digest}'" for family in families)
    script = tmp_path / "release-input.ps1"
    script.write_text(
        "param($ReleaseScript, $OutputDirectory)\n"
        f"& $ReleaseScript -BackendImage '{backend}' "
        f"-FrontendImage 'example/frontend@sha256:{digest}' "
        f"-BaseImages @({bases}) -OutputDirectory $OutputDirectory\n",
        encoding="utf-8",
    )
    output = tmp_path / "evidence"
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-File",
            str(script),
            str(ROOT / "infra/scripts/Test-ReleaseEvidence.ps1"),
            str(output),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode != 0
    expected = (
        "Exactly one base image" if invalid_input == "duplicate_family" else "pinned by digest"
    )
    assert expected in result.stderr
    assert not output.exists()
