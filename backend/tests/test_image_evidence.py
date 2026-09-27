"""CI image evidence rejects mismatched identities and scanner failures."""

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "infra/scripts/collect_image_evidence.py"
spec = importlib.util.spec_from_file_location("image_evidence", SCRIPT)
assert spec is not None and spec.loader is not None
image_evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image_evidence)
IDENTITY = "sha256:" + "a" * 64
OTHER_IDENTITY = "sha256:" + "b" * 64
NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)


def runner(
    *,
    vulnerability=False,
    scanner_error=False,
    wrong_identity=False,
    wrong_sbom_identity=False,
    empty_sbom=False,
    empty_results=False,
    empty_packages=False,
    stale_database=False,
    metadata_error=False,
    malformed_metadata=False,
    repo_digest=None,
):
    commands = []

    def fake_run(arguments):
        commands.append(arguments)
        if arguments == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(arguments, 0, "c" * 40 + "\n", "")
        if arguments == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if arguments[0] == "docker":
            return subprocess.CompletedProcess(
                arguments,
                0,
                json.dumps(
                    [
                        {
                            "Id": IDENTITY,
                            "RepoDigests": [repo_digest or "public/image@" + IDENTITY],
                        }
                    ]
                ),
                "",
            )
        if arguments == ["trivy", "version", "--format", "json"]:
            if metadata_error:
                return subprocess.CompletedProcess(arguments, 1, "", "sensitive upstream details")
            if malformed_metadata:
                return subprocess.CompletedProcess(arguments, 0, "{malformed", "")
            updated = NOW - (timedelta(days=3) if stale_database else timedelta(hours=1))
            return subprocess.CompletedProcess(
                arguments,
                0,
                json.dumps(
                    {
                        "Version": "0.74.0",
                        "VulnerabilityDB": {
                            "Version": 2,
                            "UpdatedAt": updated.isoformat(),
                            "NextUpdate": (updated + timedelta(hours=6)).isoformat(),
                            "DownloadedAt": (NOW - timedelta(minutes=30)).isoformat(),
                        },
                    }
                ),
                "",
            )
        assert arguments[-1] == IDENTITY
        assert arguments[arguments.index("--image-src") + 1] == "docker"
        output = Path(arguments[arguments.index("--output") + 1])
        if arguments[arguments.index("--format") + 1] == "cyclonedx":
            output.write_text(
                json.dumps(
                    {
                        "bomFormat": "CycloneDX",
                        "metadata": {
                            "component": {
                                "properties": [
                                    {
                                        "name": "aquasecurity:trivy:ImageID",
                                        "value": OTHER_IDENTITY
                                        if wrong_sbom_identity
                                        else IDENTITY,
                                    }
                                ]
                            }
                        },
                        "components": []
                        if empty_sbom
                        else [{"type": "library", "name": "synthetic-package"}],
                    }
                )
            )
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if scanner_error:
            return subprocess.CompletedProcess(arguments, 1, "", "sensitive upstream details")
        assert "--list-all-pkgs" in arguments
        output.write_text(
            json.dumps(
                {
                    "Metadata": {"ImageID": OTHER_IDENTITY if wrong_identity else IDENTITY},
                    "Results": []
                    if empty_results
                    else [
                        {
                            "Target": "synthetic-image",
                            "Class": "os-pkgs",
                            "Type": "synthetic",
                            "Packages": [] if empty_packages else [{"Name": "synthetic-package"}],
                            "Vulnerabilities": [
                                {
                                    "Severity": "HIGH",
                                    "VulnerabilityID": "TEST-1",
                                }
                            ]
                            if vulnerability
                            else [],
                        }
                    ],
                }
            )
        )
        return subprocess.CompletedProcess(arguments, 23 if vulnerability else 0, "", "")

    return fake_run, commands


def image_inventory(reference="public/image:synthetic"):
    return {label: reference for label in image_evidence.LABELS}


def test_manifest_binds_nonempty_reports_to_images_and_database(tmp_path, monkeypatch):
    fake_run, commands = runner()
    monkeypatch.setattr(image_evidence, "run", fake_run)
    output = tmp_path / "evidence"

    manifest = image_evidence.collect(image_inventory(), output, revision="c" * 40, now=NOW)

    assert manifest["passed"] is True
    assert manifest["schema_version"] == "ci-image-evidence-v1"
    assert manifest["trivy_version"] == "0.74.0"
    assert manifest["vulnerability_database"]["version"] == 2
    assert len(manifest["images"]) == 7
    assert all(row["image_id"] == IDENTITY for row in manifest["images"])
    assert all(row["sbom_components"] == 1 for row in manifest["images"])
    assert all(row["scanned_packages"] == 1 for row in manifest["images"])
    assert all(len(row["sbom_sha256"]) == 64 for row in manifest["images"])
    assert all(len(row["vulnerabilities_sha256"]) == 64 for row in manifest["images"])
    assert len([command for command in commands if command[0] == "docker"]) == 7
    assert commands[-1] == ["trivy", "version", "--format", "json"]


@pytest.mark.parametrize(
    ("options", "status", "error"),
    [
        ({"vulnerability": True}, "vulnerabilities_found", None),
        ({"scanner_error": True}, "error", "vulnerability_scanner_failed"),
        ({"wrong_identity": True}, "error", "report_image_identity_mismatch"),
        ({"wrong_sbom_identity": True}, "error", "sbom_image_identity_mismatch"),
        ({"empty_sbom": True}, "error", "empty_or_invalid_sbom"),
        (
            {"empty_results": True},
            "error",
            "empty_or_invalid_vulnerability_results",
        ),
        ({"empty_packages": True}, "error", "empty_vulnerability_inventory"),
    ],
)
def test_failed_or_empty_scans_never_become_success(tmp_path, monkeypatch, options, status, error):
    fake_run, _ = runner(**options)
    monkeypatch.setattr(image_evidence, "run", fake_run)

    manifest = image_evidence.collect(
        image_inventory(), tmp_path / "evidence", revision="c" * 40, now=NOW
    )

    assert manifest["passed"] is False
    assert {row["status"] for row in manifest["images"]} == {status}
    if error:
        assert {row["error_code"] for row in manifest["images"]} == {error}
    assert "sensitive upstream details" not in json.dumps(manifest)


def test_stale_vulnerability_database_is_rejected(tmp_path, monkeypatch):
    fake_run, _ = runner(stale_database=True)
    monkeypatch.setattr(image_evidence, "run", fake_run)

    with pytest.raises(image_evidence.EvidenceError, match="stale_vulnerability_database"):
        image_evidence.collect(image_inventory(), tmp_path / "evidence", revision="c" * 40, now=NOW)


def test_release_scope_requires_and_verifies_exact_repo_digests(tmp_path, monkeypatch):
    digest = "public/image@" + IDENTITY
    fake_run, _ = runner(repo_digest=digest)
    monkeypatch.setattr(image_evidence, "run", fake_run)

    manifest = image_evidence.collect(
        image_inventory(digest),
        tmp_path / "release",
        revision="c" * 40,
        scope=image_evidence.RELEASE_SCOPE,
        now=NOW,
    )

    assert manifest["passed"] is True
    assert manifest["schema_version"] == "release-image-evidence-v1"
    assert manifest["scope"] == "published-release"

    with pytest.raises(image_evidence.EvidenceError, match="release_images_require_digests"):
        image_evidence.collect(
            image_inventory(),
            tmp_path / "tagged-release",
            revision="c" * 40,
            scope=image_evidence.RELEASE_SCOPE,
            now=NOW,
        )


def test_release_scope_fails_closed_when_inspected_digest_differs(tmp_path, monkeypatch):
    requested = "public/image@" + IDENTITY
    fake_run, _ = runner(repo_digest="public/other@" + IDENTITY)
    monkeypatch.setattr(image_evidence, "run", fake_run)

    manifest = image_evidence.collect(
        image_inventory(requested),
        tmp_path / "release",
        revision="c" * 40,
        scope=image_evidence.RELEASE_SCOPE,
        now=NOW,
    )

    assert manifest["passed"] is False
    assert {row["error_code"] for row in manifest["images"]} == {"requested_digest_not_inspected"}


@pytest.mark.parametrize(
    ("runner_options", "expected_error"),
    [
        ({"metadata_error": True}, "trivy_metadata_failed"),
        ({"malformed_metadata": True}, "evidence_collection_failed"),
    ],
)
def test_cli_writes_sanitized_manifest_when_post_scan_metadata_fails(
    tmp_path, monkeypatch, capsys, runner_options, expected_error
):
    fake_run, _ = runner(**runner_options)
    monkeypatch.setattr(image_evidence, "run", fake_run)
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps({"git_revision": "c" * 40, "images": image_inventory()}),
        encoding="utf-8",
    )
    output = tmp_path / "evidence"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--input",
            str(inventory),
            "--output",
            str(output),
        ],
    )

    assert image_evidence.main() == 1
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["passed"] is False
    assert manifest["status"] == "error"
    assert manifest["error_code"] == expected_error
    assert manifest["git_revision"] == "c" * 40
    assert len(manifest["images"]) == 7
    assert {row["status"] for row in manifest["images"]} == {"passed"}
    assert "sensitive upstream details" not in json.dumps(manifest)
    assert "sensitive upstream details" not in capsys.readouterr().out


def test_cli_refuses_to_overwrite_existing_evidence_directory(tmp_path, monkeypatch, capsys):
    inventory = tmp_path / "inventory.json"
    inventory.write_text("{}", encoding="utf-8")
    output = tmp_path / "existing"
    output.mkdir()
    manifest = output / "manifest.json"
    original = '{"preserved": true}\n'
    manifest.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--input",
            str(inventory),
            "--output",
            str(output),
        ],
    )

    assert image_evidence.main() == 1
    assert manifest.read_text(encoding="utf-8") == original
    assert "could not be collected" in capsys.readouterr().out
