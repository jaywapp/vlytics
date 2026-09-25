"""Raw snapshot validation before database writes."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy import Connection

from vlytics.mirror.repositories.raw_snapshots import RawSnapshotRepository


def _add(**overrides: object) -> None:
    now = datetime.now(UTC)
    body = b"synthetic"
    values: dict[str, object] = {
        "source": "synthetic",
        "source_group_code": "001",
        "request_fingerprint": "detail:001:999:201:1",
        "redacted_url": "/synthetic",
        "requested_at": now,
        "received_at": now,
        "status_code": 200,
        "retry_after_seconds": None,
        "body_bytes": body,
        "private_uri": None,
        "sha256": hashlib.sha256(body).hexdigest(),
        "parser_version": "test-v1",
    }
    values.update(overrides)
    repository = RawSnapshotRepository(cast(Connection, None))
    repository.add(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"body_bytes": b"body", "private_uri": "private://body", "sha256": "a" * 64},
            "both bytes and a private URI",
        ),
        ({"body_bytes": b"body", "sha256": "a" * 64}, "does not match"),
        (
            {"body_bytes": None, "private_uri": "private://body", "sha256": None},
            "require a SHA-256",
        ),
        (
            {"body_bytes": None, "private_uri": None, "sha256": "a" * 64},
            "requires bytes or a private URI",
        ),
        (
            {"body_bytes": None, "private_uri": " ", "sha256": "a" * 64},
            "cannot be blank",
        ),
    ],
)
def test_invalid_raw_payload_is_rejected_before_database_access(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _add(**overrides)
