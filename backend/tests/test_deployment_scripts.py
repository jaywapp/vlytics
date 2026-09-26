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
