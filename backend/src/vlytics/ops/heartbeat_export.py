"""Read a worker heartbeat from a container and atomically export a validated copy."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_PATH = "/tmp/vlytics-worker-heartbeat.json"
MAX_BYTES = 16_384
_CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_READ_COMMAND = (
    "import sys; "
    "f = open(sys.argv[1], 'rb'); "
    f"sys.stdout.buffer.write(f.read({MAX_BYTES + 1})); "
    "f.close()"
)


def _write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".heartbeat-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validated_heartbeat(raw: bytes, now: datetime, max_age_seconds: float) -> dict[str, Any]:
    if len(raw) > MAX_BYTES:
        raise ValueError("heartbeat exceeds size limit")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != "worker-heartbeat-v1":
        raise ValueError("invalid heartbeat schema")
    completed = datetime.fromisoformat(value["completed_at"])
    if completed.tzinfo is None or completed.utcoffset() is None:
        raise ValueError("heartbeat timestamp must be aware")
    if not 0 <= (now - completed).total_seconds() <= max_age_seconds:
        raise ValueError("heartbeat is not fresh")
    for name, minimum in (("processed", 0), ("pid", 1)):
        field = value.get(name)
        if isinstance(field, bool) or not isinstance(field, int) or field < minimum:
            raise ValueError("invalid heartbeat counter")
    return {
        "schema_version": "worker-heartbeat-v1",
        "completed_at": completed.astimezone(UTC).isoformat(),
        "processed": value["processed"],
        "pid": value["pid"],
    }


def export_worker_heartbeat(
    *,
    container: str,
    output: Path,
    runtime: str = "docker",
    container_path: str = DEFAULT_PATH,
    max_age_seconds: float = 180.0,
    timeout_seconds: float = 10.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run a bounded read and replace failed evidence with an explicit unavailable marker."""
    supplied_now = now
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("export timestamp must be aware")
    code = "exported"
    try:
        if runtime not in {"docker", "podman"} or _CONTAINER.fullmatch(container) is None:
            raise ValueError("invalid container runtime or identity")
        if not container_path.startswith("/") or "\x00" in container_path:
            raise ValueError("container heartbeat path must be absolute")
        if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
            raise ValueError("max age must be finite and positive")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60:
            raise ValueError("timeout must be finite and at most 60 seconds")
        completed = subprocess.run(
            [runtime, "exec", container, "python", "-c", _READ_COMMAND, container_path],
            check=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
        now = supplied_now or datetime.now(UTC)
        evidence = _validated_heartbeat(completed.stdout, now, max_age_seconds)
    except (OSError, subprocess.SubprocessError):
        code = "source_unavailable"
        evidence = {}
    except (ValueError, TypeError, KeyError, UnicodeError):
        code = "source_invalid"
        evidence = {}
    if code != "exported":
        now = supplied_now or datetime.now(UTC)
        evidence = {
            "schema_version": "worker-heartbeat-export-failure-v1",
            "observed_at": now.astimezone(UTC).isoformat(),
            "code": code,
        }
    try:
        _write_atomic(output, evidence)
    except OSError:
        code = "output_unavailable"
    return {
        "schema_version": "worker-heartbeat-export-result-v1",
        "exported": code == "exported",
        "code": code,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime", choices=("docker", "podman"), default="docker")
    parser.add_argument("--container-path", default=DEFAULT_PATH)
    parser.add_argument("--max-age-seconds", type=float, default=180.0)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    args = parser.parse_args(argv)
    result = export_worker_heartbeat(
        container=args.container,
        output=args.output,
        runtime=args.runtime,
        container_path=args.container_path,
        max_age_seconds=args.max_age_seconds,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(result))
    return 0 if result["exported"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
