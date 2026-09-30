"""Durable provider call and cost reservations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import cast
from uuid import UUID

from sqlalchemy import Engine, text

_DATABASE_AMOUNT_QUANTUM = Decimal("0.00000001")
_DATABASE_AMOUNT_MAX = Decimal("999999999999.99999999")


def _database_amount(value: Decimal, name: str) -> Decimal:
    """Return the conservative numeric(20, 8) representation used by PostgreSQL."""

    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a finite non-negative amount")
    if value > _DATABASE_AMOUNT_MAX:
        raise ValueError(f"{name} exceeds the provider budget database range")
    try:
        normalized = value.quantize(_DATABASE_AMOUNT_QUANTUM, rounding=ROUND_CEILING)
    except InvalidOperation as error:
        raise ValueError(f"{name} cannot be represented by the provider budget database") from error
    if normalized > _DATABASE_AMOUNT_MAX:
        raise ValueError(f"{name} exceeds the provider budget database range")
    return normalized


@dataclass(frozen=True)
class ProviderBudgetLimits:
    daily_amount: Decimal
    monthly_amount: Decimal
    daily_calls: int
    monthly_calls: int
    max_calls_per_match: int
    currency: str = "USD"

    def __post_init__(self) -> None:
        if (
            not self.daily_amount.is_finite()
            or not self.monthly_amount.is_finite()
            or self.daily_amount <= 0
            or self.monthly_amount <= 0
        ):
            raise ValueError("provider budget amounts must be positive")
        if self.daily_amount > _DATABASE_AMOUNT_MAX or self.monthly_amount > _DATABASE_AMOUNT_MAX:
            raise ValueError("provider budget amounts exceed the database range")
        if self.daily_amount > self.monthly_amount:
            raise ValueError("daily provider budget must not exceed monthly budget")
        if self.daily_calls <= 0 or self.monthly_calls <= 0:
            raise ValueError("provider call limits must be positive")
        if self.daily_calls > self.monthly_calls:
            raise ValueError("daily provider call limit must not exceed monthly limit")
        if self.max_calls_per_match <= 0:
            raise ValueError("per-match provider call limit must be positive")
        if (
            len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isalpha()
            or not self.currency.isupper()
        ):
            raise ValueError("provider budget currency must be an uppercase ISO 4217 code")


class PostgresBudgetLedger:
    """One-call budget account backed by atomic PostgreSQL reservations."""

    def __init__(
        self,
        engine: Engine,
        *,
        reservation_key: str,
        provider: str,
        job_id: UUID,
        limits: ProviderBudgetLimits,
        reserved_at: datetime,
    ) -> None:
        if not reservation_key.strip() or not provider.strip():
            raise ValueError("budget reservation identity must not be blank")
        if reserved_at.tzinfo is None or reserved_at.utcoffset() is None:
            raise ValueError("reserved_at must be timezone-aware")
        self._engine = engine
        self._reservation_key = reservation_key
        self._provider = provider
        self._job_id = job_id
        self._limits = limits
        self._reserved_at = reserved_at.astimezone(UTC)
        self._reservation_id: UUID | None = None
        self._reserved_amount: Decimal | None = None

    def reserve(self, amount: Decimal) -> bool:
        normalized_amount = _database_amount(amount, "budget reservation")
        budget_day = self._reserved_at.date()
        budget_month = budget_day.replace(day=1)
        with self._engine.begin() as connection:
            match_id = connection.execute(
                text("SELECT match_id FROM ops.jobs WHERE id = :job_id"),
                {"job_id": self._job_id},
            ).scalar_one_or_none()
            match_scope = match_id or self._job_id
            for key in sorted(
                (
                    f"provider-budget:{self._provider}:day:{budget_day.isoformat()}",
                    f"provider-budget:{self._provider}:match:{match_scope}",
                    f"provider-budget:{self._provider}:month:{budget_month.isoformat()}",
                )
            ):
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": key},
                )
            monthly_currencies = (
                connection.execute(
                    text(
                        """
                    SELECT DISTINCT currency
                    FROM ops.provider_budget_reservations
                    WHERE provider = :provider AND budget_month = :budget_month
                    """
                    ),
                    {"provider": self._provider, "budget_month": budget_month},
                )
                .scalars()
                .all()
            )
            if any(str(currency) != self._limits.currency for currency in monthly_currencies):
                raise ValueError("provider monthly budget contains mixed currencies")

            existing = connection.execute(
                text(
                    """
                    SELECT id, reserved_amount, currency
                    FROM ops.provider_budget_reservations
                    WHERE reservation_key = :reservation_key
                    """
                ),
                {"reservation_key": self._reservation_key},
            ).one_or_none()
            if existing is not None:
                if str(existing[2]) != self._limits.currency:
                    raise ValueError("budget reservation replay changed its currency")
                if Decimal(existing[1]) != normalized_amount:
                    raise ValueError("budget reservation replay changed its amount")
                self._reservation_id = cast(UUID, existing[0])
                self._reserved_amount = normalized_amount
                return True

            daily_amount, daily_calls = connection.execute(
                text(
                    """
                    SELECT COALESCE(sum(COALESCE(settled_amount, reserved_amount)), 0),
                           count(*)
                    FROM ops.provider_budget_reservations
                    WHERE provider = :provider
                      AND budget_day = :budget_day
                      AND currency = :currency
                    """
                ),
                {
                    "provider": self._provider,
                    "budget_day": budget_day,
                    "currency": self._limits.currency,
                },
            ).one()
            monthly_amount, monthly_calls = connection.execute(
                text(
                    """
                    SELECT COALESCE(sum(COALESCE(settled_amount, reserved_amount)), 0),
                           count(*)
                    FROM ops.provider_budget_reservations
                    WHERE provider = :provider
                      AND budget_month = :budget_month
                      AND currency = :currency
                    """
                ),
                {
                    "provider": self._provider,
                    "budget_month": budget_month,
                    "currency": self._limits.currency,
                },
            ).one()
            match_calls = connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM ops.provider_budget_reservations reservations
                    JOIN ops.jobs jobs ON jobs.id = reservations.job_id
                    WHERE reservations.provider = :provider
                      AND (
                          (:match_id IS NOT NULL AND jobs.match_id = :match_id)
                          OR (:match_id IS NULL AND reservations.job_id = :job_id)
                      )
                    """
                ),
                {"provider": self._provider, "match_id": match_id, "job_id": self._job_id},
            ).scalar_one()
            if (
                Decimal(daily_amount) + normalized_amount > self._limits.daily_amount
                or Decimal(monthly_amount) + normalized_amount > self._limits.monthly_amount
                or int(daily_calls) >= self._limits.daily_calls
                or int(monthly_calls) >= self._limits.monthly_calls
                or int(match_calls) >= self._limits.max_calls_per_match
            ):
                return False
            reservation_id = connection.execute(
                text(
                    """
                    INSERT INTO ops.provider_budget_reservations (
                        reservation_key, provider, job_id, reserved_at,
                        budget_day, budget_month, reserved_amount, currency
                    ) VALUES (
                        :reservation_key, :provider, :job_id, :reserved_at,
                        :budget_day, :budget_month, :reserved_amount, :currency
                    )
                    RETURNING id
                    """
                ),
                {
                    "reservation_key": self._reservation_key,
                    "provider": self._provider,
                    "job_id": self._job_id,
                    "reserved_at": self._reserved_at,
                    "budget_day": budget_day,
                    "budget_month": budget_month,
                    "reserved_amount": normalized_amount,
                    "currency": self._limits.currency,
                },
            ).scalar_one()
        self._reservation_id = cast(UUID, reservation_id)
        self._reserved_amount = normalized_amount
        return True

    def settle(self, reserved: Decimal, actual: Decimal) -> None:
        normalized_reserved = _database_amount(reserved, "reserved provider cost")
        normalized_actual = _database_amount(actual, "actual provider cost")
        if self._reservation_id is None or self._reserved_amount != normalized_reserved:
            raise ValueError("provider cost reservation was not found")
        with self._engine.begin() as connection:
            result = connection.execute(
                text(
                    """
                    UPDATE ops.provider_budget_reservations
                    SET settled_amount = :actual,
                        state = 'settled',
                        conservative_charge = :conservative_charge,
                        settled_at = :settled_at
                    WHERE id = :reservation_id
                      AND state = 'reserved'
                      AND reserved_amount = :reserved
                    """
                ),
                {
                    "reservation_id": self._reservation_id,
                    "reserved": normalized_reserved,
                    "actual": normalized_actual,
                    "conservative_charge": normalized_actual >= normalized_reserved,
                    "settled_at": datetime.now(UTC),
                },
            )
            if result.rowcount == 0:
                existing = connection.execute(
                    text(
                        """
                        SELECT settled_amount
                        FROM ops.provider_budget_reservations
                        WHERE id = :reservation_id AND state = 'settled'
                        """
                    ),
                    {"reservation_id": self._reservation_id},
                ).scalar_one_or_none()
                if existing is None or Decimal(existing) != normalized_actual:
                    raise ValueError("provider budget settlement conflicted")

    def release(self, reserved: Decimal) -> None:
        normalized_reserved = _database_amount(reserved, "budget release")
        if self._reservation_id is None or self._reserved_amount != normalized_reserved:
            raise ValueError("provider cost reservation was not found")
        self.settle(normalized_reserved, Decimal("0"))
