"""Receipt-first V-Mirror ingestion orchestration."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Protocol
from uuid import UUID

from vlytics.mirror.models import (
    Availability,
    ContractError,
    ContractIssue,
    FactRevisionBatch,
    IngestAction,
    IngestResult,
    RawReceipt,
    SourceRequestKey,
    SourceResponse,
)
from vlytics.mirror.parser import KovoParser
from vlytics.mirror.repositories.facts import PersistResult


class RawReceiptStore(Protocol):
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
    ) -> UUID: ...


class FactStore(Protocol):
    def persist_durable(self, batch: FactRevisionBatch) -> PersistResult: ...

    def quarantine_durable(
        self,
        *,
        receipt_id: UUID,
        source: str,
        observed_at: datetime,
        request_key: SourceRequestKey,
        issues: tuple[ContractIssue, ...],
    ) -> None: ...


class MirrorIngestionService:
    """Commit a receipt first, then parse and commit facts separately."""

    def __init__(
        self,
        raw_snapshots: RawReceiptStore,
        facts: FactStore,
        parser: KovoParser,
    ) -> None:
        self._raw_snapshots = raw_snapshots
        self._facts = facts
        self._parser = parser

    def ingest(self, response: SourceResponse) -> IngestResult:
        """A parser or fact failure can never roll back the receipt."""

        stored_body, stored_uri, body_hash, validation_issues = self._prepare_raw(response)
        receipt_id = self._raw_snapshots.add_durable(
            source=response.source,
            source_group_code=response.request_key.gcode,
            request_fingerprint=response.request_key.fingerprint(),
            redacted_url=response.redacted_url,
            requested_at=response.requested_at,
            received_at=response.received_at,
            status_code=response.status_code,
            retry_after_seconds=response.retry_after_seconds,
            body_bytes=stored_body,
            private_uri=stored_uri,
            sha256=body_hash,
            parser_version=self._parser.parser_version,
        )
        receipt_response = SourceResponse(
            source=response.source,
            request_key=response.request_key,
            redacted_url=response.redacted_url,
            requested_at=response.requested_at,
            received_at=response.received_at,
            status_code=response.status_code,
            body_bytes=stored_body,
            retry_after_seconds=response.retry_after_seconds,
            private_uri=stored_uri,
            body_sha256=body_hash,
        )
        receipt = RawReceipt(
            receipt_id,
            receipt_response,
            body_hash,
            self._parser.parser_version,
        )
        if validation_issues:
            self._quarantine(receipt, validation_issues)
            return IngestResult(receipt.id, IngestAction.QUARANTINE, 0, validation_issues)
        if response.status_code == 429:
            return IngestResult(receipt.id, IngestAction.RETRY_LATER, 0)
        if response.status_code != 200:
            issues = (
                ContractIssue(
                    "http_status_not_parseable",
                    f"HTTP {response.status_code!r} must not be normalized",
                    "$",
                    Availability.UNVERIFIED,
                ),
            )
            self._quarantine(receipt, issues)
            return IngestResult(receipt.id, IngestAction.QUARANTINE, 0, issues)
        try:
            batch = self._parser.parse(receipt)
        except ContractError as error:
            self._quarantine(receipt, error.issues)
            return IngestResult(receipt.id, IngestAction.QUARANTINE, 0, error.issues)

        try:
            persisted = self._facts.persist_durable(batch)
        except Exception:
            failure = (
                ContractIssue(
                    "fact_persistence_failed",
                    "normalized fact transaction rolled back after receipt commit",
                    "$",
                    Availability.UNVERIFIED,
                ),
            )
            self._quarantine(receipt, failure)
            raise
        if batch.issues:
            return IngestResult(
                receipt.id,
                IngestAction.QUARANTINE,
                persisted.fact_revisions,
                batch.issues,
            )
        action = (
            IngestAction.APPEND_REVISION
            if persisted.fact_revisions
            else IngestAction.APPEND_RECEIPT_ONLY
        )
        return IngestResult(receipt.id, action, persisted.fact_revisions)

    def _quarantine(self, receipt: RawReceipt, issues: tuple[ContractIssue, ...]) -> None:
        self._facts.quarantine_durable(
            receipt_id=receipt.id,
            source=receipt.response.source,
            observed_at=receipt.response.received_at,
            request_key=receipt.response.request_key,
            issues=issues,
        )

    @staticmethod
    def _prepare_raw(
        response: SourceResponse,
    ) -> tuple[bytes | None, str | None, str | None, tuple[ContractIssue, ...]]:
        issues: list[ContractIssue] = []
        if response.body_bytes is not None:
            actual_hash = hashlib.sha256(response.body_bytes).hexdigest()
            if response.body_sha256 is not None and response.body_sha256 != actual_hash:
                issues.append(
                    ContractIssue(
                        "body_hash_mismatch",
                        "provided body SHA-256 does not match response bytes",
                        "$.body_sha256",
                        Availability.UNVERIFIED,
                    )
                )
            if response.private_uri is not None:
                issues.append(
                    ContractIssue(
                        "ambiguous_raw_body",
                        "response supplied both bytes and a private URI; bytes were retained",
                        "$",
                        Availability.UNVERIFIED,
                    )
                )
            return response.body_bytes, None, actual_hash, tuple(issues)

        private_uri = response.private_uri
        claimed_hash = response.body_sha256
        valid_hash = MirrorIngestionService._valid_hash(claimed_hash)
        if private_uri is not None and not private_uri.strip():
            issues.append(
                ContractIssue(
                    "blank_private_uri",
                    "blank private URI cannot identify raw content",
                    "$.private_uri",
                    Availability.MISSING,
                )
            )
            private_uri = None
        if private_uri is not None and not valid_hash:
            issues.append(
                ContractIssue(
                    "invalid_private_body_hash",
                    "private raw content requires a lowercase SHA-256 digest",
                    "$.body_sha256",
                    Availability.MISSING,
                )
            )
            private_uri = None
            claimed_hash = None
        elif private_uri is None and claimed_hash is not None:
            issues.append(
                ContractIssue(
                    "hash_without_body",
                    "body hash was supplied without bytes or a private URI",
                    "$.body_sha256",
                    Availability.MISSING,
                )
            )
            claimed_hash = None
        return None, private_uri, claimed_hash, tuple(issues)

    @staticmethod
    def _valid_hash(value: str | None) -> bool:
        return (
            value is not None
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )
