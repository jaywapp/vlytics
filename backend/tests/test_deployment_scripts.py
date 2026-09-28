"""Run the package checker against both checkout newline styles."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed")
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["LF", "CRLF"])
def test_package_validator_accepts_checkout_newlines(tmp_path, newline):
    files = [
        "infra/scripts/Test-DeploymentPackage.ps1",
        "infra/scripts/Backup-Database.ps1",
        "infra/scripts/Invoke-RestoreDrill.ps1",
        "infra/scripts/Invoke-Preflight.ps1",
        "infra/postgres-init/001-create-migrator.sql",
        "infra/compose.production.yaml",
        "infra/.env.example",
        "infra/operational.production.example.toml",
        "frontend/Dockerfile",
        "frontend/nginx.production.conf",
        "infra/nginx.operator-ingress.conf",
        "frontend/.dockerignore",
        "contracts/config.schema.json",
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
