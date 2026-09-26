"""File heartbeat for completed worker polls and container health checks."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path


def record_heartbeat(path: Path, *, now: datetime, processed: int) -> None:
    if now.tzinfo is None:
        raise ValueError("Heartbeat timestamps must include a timezone")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "schema_version": "worker-heartbeat-v1",
                "completed_at": now.isoformat(),
                "processed": processed,
                "pid": os.getpid(),
            }
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def heartbeat_is_fresh(path: Path, *, now: datetime, max_age: timedelta) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema_version") != "worker-heartbeat-v1":
            return False
        completed = datetime.fromisoformat(value["completed_at"])
        return timedelta(0) <= now - completed <= max_age
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Check completed worker poll freshness")
    parser.add_argument(
        "--path",
        type=Path,
        default=os.getenv("VLYTICS_WORKER_HEARTBEAT_PATH", "/tmp/vlytics-worker-heartbeat.json"),
    )
    parser.add_argument("--max-age-seconds", type=float, default=300)
    args = parser.parse_args()
    if args.max_age_seconds <= 0:
        return 1
    return (
        0
        if heartbeat_is_fresh(
            args.path, now=datetime.now(UTC), max_age=timedelta(seconds=args.max_age_seconds)
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
