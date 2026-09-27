"""Record SBOM and vulnerability evidence for immutable Docker images."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

LABELS = {"backend", "frontend", "python", "uv", "postgres", "node", "nginx"}
IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}")
IMAGE_REFERENCE = re.compile(r"[a-z0-9][a-z0-9./:@_-]+")
DIGEST_REFERENCE = re.compile(r"[a-z0-9][a-z0-9./:_-]+@sha256:[a-f0-9]{64}")
VULNERABILITY_EXIT = 23
MAX_DATABASE_AGE = timedelta(hours=48)
FUTURE_TOLERANCE = timedelta(minutes=5)
CI_SCOPE = "built-ci-images-not-published-release"
RELEASE_SCOPE = "published-release"
SCHEMAS = {
    CI_SCOPE: "ci-image-evidence-v1",
    RELEASE_SCOPE: "release-image-evidence-v1",
}


class EvidenceError(ValueError):
    """A sanitized evidence contract failure."""

    def __init__(
        self, code: str, *, records: list[dict[str, Any]] | None = None
    ) -> None:
        super().__init__(code)
        self.code = code
        self.records = records or []


def run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments, capture_output=True, text=True, check=False, timeout=600
    )


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json_object(value: str, error_code: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise EvidenceError(error_code)
    return parsed


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise EvidenceError("invalid_vulnerability_database_metadata")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceError("invalid_vulnerability_database_metadata") from error
    if parsed.tzinfo is None:
        raise EvidenceError("invalid_vulnerability_database_metadata")
    return parsed.astimezone(UTC)


def _trivy_metadata(*, now: datetime) -> dict[str, Any]:
    result = run(["trivy", "version", "--format", "json"])
    if result.returncode:
        raise EvidenceError("trivy_metadata_failed")
    data = _json_object(result.stdout, "invalid_trivy_metadata")
    version = data.get("Version")
    database = data.get("VulnerabilityDB")
    if not isinstance(version, str) or not version or not isinstance(database, dict):
        raise EvidenceError("invalid_vulnerability_database_metadata")
    database_version = database.get("Version")
    if not isinstance(database_version, int) or isinstance(database_version, bool):
        raise EvidenceError("invalid_vulnerability_database_metadata")
    updated_at = _timestamp(database.get("UpdatedAt"))
    next_update = _timestamp(database.get("NextUpdate"))
    downloaded_at = _timestamp(database.get("DownloadedAt"))
    current = now.astimezone(UTC)
    if (
        updated_at > current + FUTURE_TOLERANCE
        or downloaded_at > current + FUTURE_TOLERANCE
        or current - updated_at > MAX_DATABASE_AGE
        or next_update <= updated_at
    ):
        raise EvidenceError("stale_vulnerability_database")
    return {
        "version": version,
        "vulnerability_database": {
            "version": database_version,
            "updated_at": database["UpdatedAt"],
            "next_update": database["NextUpdate"],
            "downloaded_at": database["DownloadedAt"],
        },
    }


def _sbom_components(data: dict[str, Any], image_id: str) -> list[dict[str, Any]]:
    if data.get("bomFormat") != "CycloneDX":
        raise EvidenceError("invalid_sbom_format")
    metadata = data.get("metadata")
    component = metadata.get("component") if isinstance(metadata, dict) else None
    properties = component.get("properties") if isinstance(component, dict) else None
    if not isinstance(properties, list) or any(
        not isinstance(item, dict) for item in properties
    ):
        raise EvidenceError("invalid_sbom_image_identity")
    identities = [
        item.get("value")
        for item in properties
        if item.get("name") == "aquasecurity:trivy:ImageID"
    ]
    if identities != [image_id]:
        raise EvidenceError("sbom_image_identity_mismatch")
    components = data.get("components")
    if (
        not isinstance(components, list)
        or not components
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not item["name"]
            for item in components
        )
    ):
        raise EvidenceError("empty_or_invalid_sbom")
    return components


def _vulnerability_findings(
    data: dict[str, Any], image_id: str
) -> tuple[list[dict[str, Any]], int, int]:
    metadata = data.get("Metadata")
    if not isinstance(metadata, dict) or metadata.get("ImageID") != image_id:
        raise EvidenceError("report_image_identity_mismatch")
    results = data.get("Results")
    if not isinstance(results, list) or not results:
        raise EvidenceError("empty_or_invalid_vulnerability_results")
    findings: list[dict[str, Any]] = []
    package_count = 0
    for result in results:
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("Target"), str)
            or not result["Target"]
            or not isinstance(result.get("Class"), str)
            or not result["Class"]
        ):
            raise EvidenceError("empty_or_invalid_vulnerability_results")
        packages = result.get("Packages")
        if packages is not None and (
            not isinstance(packages, list)
            or any(not isinstance(package, dict) for package in packages)
        ):
            raise EvidenceError("empty_or_invalid_vulnerability_results")
        package_count += len(packages or [])
        vulnerabilities = result.get("Vulnerabilities")
        if vulnerabilities is None:
            vulnerabilities = []
        if not isinstance(vulnerabilities, list) or any(
            not isinstance(vulnerability, dict) for vulnerability in vulnerabilities
        ):
            raise EvidenceError("empty_or_invalid_vulnerability_results")
        for vulnerability in vulnerabilities:
            if (
                vulnerability.get("Severity") not in {"HIGH", "CRITICAL"}
                or not isinstance(vulnerability.get("VulnerabilityID"), str)
                or not vulnerability["VulnerabilityID"]
            ):
                raise EvidenceError("invalid_vulnerability_finding")
            findings.append(vulnerability)
    if package_count == 0:
        raise EvidenceError("empty_vulnerability_inventory")
    return findings, len(results), package_count


def scan_one(
    label: str,
    reference: str,
    directory: Path,
    *,
    require_repo_digest: bool = False,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "label": label,
        "requested_reference": reference,
        "status": "error",
    }
    try:
        inspection = run(["docker", "image", "inspect", reference])
        if inspection.returncode:
            raise EvidenceError("image_inspection_failed")
        inspected_list = json.loads(inspection.stdout)
        if (
            not isinstance(inspected_list, list)
            or len(inspected_list) != 1
            or not isinstance(inspected_list[0], dict)
        ):
            raise EvidenceError("invalid_image_inspection")
        inspected = inspected_list[0]
        image_id = inspected.get("Id")
        if not isinstance(image_id, str) or IMAGE_ID.fullmatch(image_id) is None:
            raise EvidenceError("invalid_image_identity")
        repo_digests = inspected.get("RepoDigests") or []
        if not isinstance(repo_digests, list) or any(
            not isinstance(digest, str) for digest in repo_digests
        ):
            raise EvidenceError("invalid_repo_digests")
        if require_repo_digest and reference not in repo_digests:
            raise EvidenceError("requested_digest_not_inspected")
        record.update(image_id=image_id, repo_digests=repo_digests)
        sbom = directory / f"{label}.cdx.json"
        report = directory / f"{label}.vulnerabilities.json"
        common = [
            "trivy",
            "image",
            "--quiet",
            "--disable-telemetry",
            "--image-src",
            "docker",
        ]
        sbom_result = run(
            [*common, "--format", "cyclonedx", "--output", str(sbom), image_id]
        )
        if sbom_result.returncode:
            raise EvidenceError("sbom_generation_failed")
        sbom_data = _json_object(
            sbom.read_text(encoding="utf-8"), "invalid_sbom_format"
        )
        components = _sbom_components(sbom_data, image_id)
        record.update(sbom_sha256=sha256(sbom), sbom_components=len(components))
        scan = run(
            [
                *common,
                "--scanners",
                "vuln",
                "--list-all-pkgs",
                "--severity",
                "HIGH,CRITICAL",
                "--exit-code",
                str(VULNERABILITY_EXIT),
                "--format",
                "json",
                "--output",
                str(report),
                image_id,
            ]
        )
        if scan.returncode not in {0, VULNERABILITY_EXIT}:
            raise EvidenceError("vulnerability_scanner_failed")
        data = _json_object(
            report.read_text(encoding="utf-8"), "invalid_vulnerability_report"
        )
        findings, target_count, package_count = _vulnerability_findings(data, image_id)
        if bool(findings) != (scan.returncode == VULNERABILITY_EXIT):
            raise EvidenceError("scanner_exit_report_mismatch")
        record.update(
            vulnerabilities_sha256=sha256(report),
            scanned_targets=target_count,
            scanned_packages=package_count,
            high_critical_count=len(findings),
            status="vulnerabilities_found" if findings else "passed",
        )
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        AttributeError,
    ) as error:
        record["error_code"] = (
            error.code
            if isinstance(error, EvidenceError)
            else "evidence_collection_failed"
        )
    return record


def _validate_inventory(
    images: object, *, revision: object, scope: str
) -> dict[str, str]:
    if scope not in SCHEMAS:
        raise EvidenceError("invalid_evidence_scope")
    if (
        not isinstance(images, dict)
        or set(images) != LABELS
        or not isinstance(revision, str)
        or re.fullmatch(r"[a-f0-9]{40}", revision) is None
        or any(not isinstance(label, str) for label in images)
    ):
        raise EvidenceError("invalid_image_inventory")
    typed_images = cast(dict[str, object], images)
    if any(
        not isinstance(ref, str) or IMAGE_REFERENCE.fullmatch(ref) is None
        for ref in typed_images.values()
    ):
        raise EvidenceError("invalid_image_reference")
    validated = cast(dict[str, str], images)
    if scope == RELEASE_SCOPE and any(
        DIGEST_REFERENCE.fullmatch(ref) is None for ref in validated.values()
    ):
        raise EvidenceError("release_images_require_digests")
    return validated


def _collect_prepared(
    images: dict[str, str],
    directory: Path,
    *,
    revision: str,
    scope: str,
    now: datetime | None,
) -> dict[str, Any]:
    records = [
        scan_one(
            label,
            images[label],
            directory,
            require_repo_digest=scope == RELEASE_SCOPE,
        )
        for label in sorted(LABELS)
    ]
    current = now or datetime.now(UTC)
    try:
        trivy = _trivy_metadata(now=current)
    except Exception as error:
        error_code = (
            error.code
            if isinstance(error, EvidenceError)
            else "evidence_collection_failed"
        )
        raise EvidenceError(error_code, records=records) from error
    manifest = {
        "schema_version": SCHEMAS[scope],
        "git_revision": revision,
        "created_at_utc": current.astimezone(UTC).isoformat(),
        "trivy_version": trivy["version"],
        "vulnerability_database": trivy["vulnerability_database"],
        "scope": scope,
        "images": records,
        "passed": all(record["status"] == "passed" for record in records),
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def collect(
    images: dict[str, str],
    directory: Path,
    *,
    revision: str,
    scope: str = CI_SCOPE,
    now: datetime | None = None,
) -> dict[str, Any]:
    validated = _validate_inventory(images, revision=revision, scope=scope)
    directory.mkdir(parents=True, exist_ok=False)
    return _collect_prepared(
        validated,
        directory,
        revision=revision,
        scope=scope,
        now=now,
    )


def _write_failure_manifest(
    directory: Path,
    *,
    scope: str,
    error_code: str,
    revision: str | None,
    images: list[dict[str, Any]],
) -> None:
    manifest: dict[str, Any] = {
        "schema_version": SCHEMAS[scope],
        "git_revision": revision,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "scope": scope,
        "images": images,
        "status": "error",
        "error_code": error_code,
        "passed": False,
    }
    with (directory / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2)
        stream.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scope", choices=tuple(SCHEMAS), default=CI_SCOPE)
    arguments = parser.parse_args()
    revision_for_failure: str | None = None
    output_created = False
    try:
        arguments.output.mkdir(parents=True, exist_ok=False)
        output_created = True
        inventory = _json_object(
            arguments.input.read_text(encoding="utf-8"), "invalid_input"
        )
        inventory_revision = inventory.get("git_revision")
        if isinstance(inventory_revision, str) and re.fullmatch(
            r"[a-f0-9]{40}", inventory_revision
        ):
            revision_for_failure = inventory_revision
        revision = run(["git", "rev-parse", "HEAD"])
        changes = run(["git", "status", "--porcelain"])
        if (
            revision.returncode
            or changes.returncode
            or changes.stdout.strip()
            or revision.stdout.strip() != inventory_revision
        ):
            raise EvidenceError("checkout_provenance_mismatch")
        images = _validate_inventory(
            inventory.get("images"),
            revision=inventory_revision,
            scope=arguments.scope,
        )
        manifest = _collect_prepared(
            images,
            arguments.output,
            revision=cast(str, inventory_revision),
            scope=arguments.scope,
            now=None,
        )
    except Exception as error:
        error_code = (
            error.code
            if isinstance(error, EvidenceError)
            else "evidence_collection_failed"
        )
        if output_created:
            try:
                _write_failure_manifest(
                    arguments.output,
                    scope=arguments.scope,
                    error_code=error_code,
                    revision=revision_for_failure,
                    images=error.records if isinstance(error, EvidenceError) else [],
                )
            except Exception:
                pass
        print(
            "Image evidence could not be collected; inspect tool availability and input contract."
        )
        return 1
    for record in manifest["images"]:
        print(f"{record['label']}: {record['status']}")
    return 0 if manifest["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
