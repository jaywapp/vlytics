from decimal import Decimal

import pytest

from vlytics.engine.providers.budget import ProviderBudgetLimits, _database_amount


def test_database_amount_rounds_up_to_postgres_scale() -> None:
    assert _database_amount(
        Decimal("28.0024691356"),
        "test amount",
    ) == Decimal("28.00246914")
    assert _database_amount(Decimal("0"), "test amount") == Decimal("0E-8")


def test_database_amount_rounding_cannot_slip_past_a_cap_boundary() -> None:
    cap = Decimal("1.000000005")
    normalized = _database_amount(Decimal("1.000000001"), "test reservation")

    assert normalized == Decimal("1.00000001")
    assert normalized > cap


@pytest.mark.parametrize(
    "value",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("-0.000000001"),
        Decimal("1000000000000"),
    ],
)
def test_database_amount_rejects_non_finite_negative_and_out_of_range_values(
    value: Decimal,
) -> None:
    with pytest.raises(ValueError):
        _database_amount(value, "test amount")


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity")])
def test_provider_budget_limits_reject_non_finite_amounts(value: Decimal) -> None:
    with pytest.raises(ValueError, match="amounts"):
        ProviderBudgetLimits(
            daily_amount=value,
            monthly_amount=value,
            daily_calls=1,
            monthly_calls=1,
            max_calls_per_match=1,
        )
