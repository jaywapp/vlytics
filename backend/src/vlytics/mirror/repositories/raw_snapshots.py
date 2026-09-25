"""Durable raw observation repository."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import Connection, Engine, text


class RawSnapshotRepository:
    """Persist every raw receipt in its own committed transaction."""

    def __init__(self, bind: Engine | Connection) -> None:
        self._bind = bind

    @property
    def _engine(self) -> Engine:
        return self._bind if isinstance(self._bind, Engine) else self._bind.engine

    def add_durable(
        self,
        *,
        source: str,
        source_group_code: str,
        request_fingerprint: str,
        redacted_url: str,
        requested_at: datetime,
        received_at: datetime,
        status_code: int | None,
        retry_after_seconds: int | None,
        body_bytes: bytes | None,
        private_uri: str | None,
        sha256: str | None,
        parser_version: str,
    ) -> UUID:
        """Commit a receipt before parsing or fact persistence starts."""

        with self._engine.begin() as connection:
            return RawSnapshotRepository(connection).add(
                source=source,
                source_group_code=source_group_code,
                request_fingerprint=request_fingerprint,
                redacted_url=redacted_url,
                requested_at=requested_at,
                received_at=received_at,
                status_code=status_code,
                retry_after_seconds=retry_after_seconds,
                body_bytes=body_bytes,
                private_uri=private_uri,
                sha256=sha256,
                parser_version=parser_version,
            )

    def add(
        self,
        *,
        source: str,
        source_group_code: str,
        request_fingerprint: str,
        redacted_url: str,
        requested_at: datetime,
        received_at: datetime,
        status_code: int | None,
        retry_after_seconds: int | None,
        body_bytes: bytes | None,
        private_uri: str | None,
        sha256: str | None,
        parser_version: str,
    ) -> UUID:
        """Insert inside the caller transaction; ingestion uses add_durable instead."""

        self._validate_payload(body_bytes, private_uri, sha256)
        if not isinstance(self._bind, Connection):
            raise TypeError("add requires a Connection; use add_durable with an Engine")
        snapshot_id = self._bind.execute(
            text(
                """
                INSERT INTO mirror.raw_snapshots (
                    source, source_group_code, request_fingerprint, redacted_url,
                    requested_at, received_at, status_code, retry_after_seconds,
                    body_bytes, private_uri, sha256, parser_version
                ) VALUES (
                    :source, :source_group_code, :request_fingerprint, :redacted_url,
                    :requested_at, :received_at, :status_code, :retry_after_seconds,
                    :body_bytes, :private_uri, :sha256, :parser_version
                )
                RETURNING id
                """
            ),
            {
                "source": source,
                "source_group_code": source_group_code,
                "request_fingerprint": request_fingerprint,
                "redacted_url": redacted_url,
                "requested_at": requested_at,
                "received_at": received_at,
                "status_code": status_code,
                "retry_after_seconds": retry_after_seconds,
                "body_bytes": body_bytes,
                "private_uri": private_uri,
                "sha256": sha256,
                "parser_version": parser_version,
            },
        ).scalar_one()
        return cast(UUID, snapshot_id)

    @staticmethod
    def _validate_payload(
        body_bytes: bytes | None,
        private_uri: str | None,
        sha256: str | None,
    ) -> None:
        if body_bytes is not None and private_uri is not None:
            raise ValueError("raw snapshots cannot contain both bytes and a private URI")
        if private_uri is not None and not private_uri.strip():
            raise ValueError("raw snapshot private URI cannot be blank")
        if sha256 is not None and (
            len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise ValueError("raw snapshot SHA-256 must be 64 lowercase hexadecimal characters")
        if body_bytes is not None:
            expected = hashlib.sha256(body_bytes).hexdigest()
            if sha256 != expected:
                raise ValueError("raw snapshot SHA-256 does not match body bytes")
        elif private_uri is not None:
            if sha256 is None:
                raise ValueError("private raw snapshots require a SHA-256 digest")
        elif sha256 is not None:
            raise ValueError("raw snapshot SHA-256 requires bytes or a private URI")
