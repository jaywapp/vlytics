from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine, text

from vlytics.ops.health_evidence import collect_database_evidence


def test_health_evidence_reads_running_leases_and_budgets_as_read_api(
    postgres_role_urls: object,
) -> None:
    now = datetime.now(UTC)
    job_id = uuid4()
    suffix = uuid4().hex
    engine = create_engine(str(postgres_role_urls.engine))
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO ops.jobs (
                        id, job_key, job_type, due_at, state, lease_owner, lease_until,
                        lease_started_at
                    ) VALUES (
                        :id, :job_key, 'health-evidence-test', :due_at, 'running',
                        'health-evidence-test', :lease_until, :lease_started_at
                    )
                    """
                ),
                {
                    "id": job_id,
                    "job_key": f"health-evidence:{suffix}",
                    "due_at": now - timedelta(minutes=1),
                    "lease_started_at": now - timedelta(seconds=30),
                    "lease_until": now + timedelta(minutes=5),
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO ops.provider_budget_reservations (
                        reservation_key, provider, job_id, reserved_at, budget_day,
                        budget_month, reserved_amount, currency
                    ) VALUES (
                        :reservation_key, 'openai', :job_id, :reserved_at, :budget_day,
                        :budget_month, :reserved_amount, 'USD'
                    )
                    """
                ),
                {
                    "reservation_key": f"health-evidence:{suffix}",
                    "job_id": job_id,
                    "reserved_at": now,
                    "budget_day": now.date(),
                    "budget_month": now.date().replace(day=1),
                    "reserved_amount": Decimal("0.125"),
                },
            )

        evidence = collect_database_evidence(database_url=str(postgres_role_urls.read_api), now=now)

        assert any(row["job_id"] == str(job_id) for row in evidence.running_jobs)
        openai = next(row for row in evidence.provider_usage if row["provider"] == "openai")
        assert Decimal(str(openai["daily_used_amount"])) >= Decimal("0.125")
        assert int(openai["daily_used_calls"]) >= 1
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE ops.jobs
                    SET state = 'quarantined', lease_owner = NULL, lease_until = NULL,
                        lease_started_at = NULL, error_code = 'test_cleanup'
                    WHERE id = :id AND state = 'running'
                    """
                ),
                {"id": job_id},
            )
        engine.dispose()
