"""Market adapter boundary with a safe missing default and synthetic implementation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from vlytics.engine.market.models import (
    AvailabilityStatus,
    MarketAvailability,
    MarketContractError,
    MarketSnapshot,
)


class MarketAdapter(Protocol):
    def snapshot_as_of(
        self,
        *,
        match_id: str,
        cutoff_at: datetime,
        max_age: timedelta,
    ) -> MarketAvailability: ...


class PersistedMarketRepository(Protocol):
    def availability_as_of(
        self,
        *,
        match_id: UUID,
        cutoff_at: datetime,
        max_age: timedelta,
    ) -> MarketAvailability: ...


class StoredMarketAdapter:
    """Read persisted snapshots through the repository's as-of state contract."""

    def __init__(self, repository: PersistedMarketRepository) -> None:
        self._repository = repository

    def snapshot_as_of(
        self,
        *,
        match_id: str,
        cutoff_at: datetime,
        max_age: timedelta,
    ) -> MarketAvailability:
        return self._repository.availability_as_of(
            match_id=UUID(match_id),
            cutoff_at=cutoff_at,
            max_age=max_age,
        )


class MissingMarketAdapter:
    """Default while OP-005 has no approved external schema or connection."""

    def snapshot_as_of(
        self,
        *,
        match_id: str,
        cutoff_at: datetime,
        max_age: timedelta,
    ) -> MarketAvailability:
        del match_id, cutoff_at, max_age
        return MarketAvailability(
            status=AvailabilityStatus.MISSING,
            reason="external_market_adapter_not_configured",
        )


class SyntheticMarketAdapter:
    """In-memory adapter restricted to fixtures and deterministic contract tests."""

    def __init__(
        self,
        snapshots: Sequence[MarketSnapshot],
        *,
        source_event_mappings: Mapping[tuple[str, str], str],
    ) -> None:
        self._snapshots = tuple(snapshots)
        self._mappings = dict(source_event_mappings)
        for snapshot in self._snapshots:
            mapped_match = self._mappings.get((snapshot.source, snapshot.source_event_id))
            if mapped_match is None:
                raise MarketContractError("synthetic source event requires an explicit mapping")
            if mapped_match != snapshot.match_id:
                raise MarketContractError("synthetic source event maps to another match")

    def snapshot_as_of(
        self,
        *,
        match_id: str,
        cutoff_at: datetime,
        max_age: timedelta,
    ) -> MarketAvailability:
        if cutoff_at.tzinfo is None or cutoff_at.utcoffset() is None:
            raise MarketContractError("cutoff_at must be timezone-aware")
        if max_age < timedelta(0):
            raise MarketContractError("max_age must be non-negative")
        matching = tuple(snapshot for snapshot in self._snapshots if snapshot.match_id == match_id)
        eligible = tuple(
            snapshot
            for snapshot in matching
            if snapshot.quoted_at <= cutoff_at
            and snapshot.observed_at <= cutoff_at
            and snapshot.received_at <= cutoff_at
        )
        if not eligible:
            reason = (
                "late_quote_received_after_cutoff"
                if any(
                    snapshot.quoted_at <= cutoff_at and snapshot.received_at > cutoff_at
                    for snapshot in matching
                )
                else "no_market_snapshot_as_of_cutoff"
            )
            return MarketAvailability(status=AvailabilityStatus.MISSING, reason=reason)
        selected = max(
            eligible,
            key=lambda snapshot: (
                snapshot.quoted_at,
                snapshot.observed_at,
                snapshot.received_at,
                snapshot.snapshot_id,
            ),
        )
        if cutoff_at - selected.quoted_at > max_age:
            return MarketAvailability(
                status=AvailabilityStatus.STALE,
                reason="latest_quote_exceeds_max_age",
            )
        return MarketAvailability(
            status=AvailabilityStatus.AVAILABLE,
            reason="latest_quote_available_as_of_cutoff",
            snapshot=selected,
        )


def default_market_adapter() -> MarketAdapter:
    """Return missing until OP-005 supplies and validates a real adapter."""

    return MissingMarketAdapter()
