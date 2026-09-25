"""PostgreSQL contract for season identity fact counting."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from vlytics.mirror.ingestion import MirrorIngestionService
from vlytics.mirror.models import IngestAction, SourceEndpoint, SourceRequestKey, SourceResponse
from vlytics.mirror.parser import KovoParser
from vlytics.mirror.repositories.facts import MirrorFactRepository
from vlytics.mirror.repositories.raw_snapshots import RawSnapshotRepository


class PostgresRoleUrls(Protocol):
    collector: str


FIXTURE = (
    Path(__file__).resolve().parents[3] / "fixtures" / "synthetic" / "kovo-season-list.v1.json"
)


def test_first_season_list_is_a_fact_then_repeated_body_is_receipt_only(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    engine = create_engine(
        make_url(postgres_role_urls.collector).set(drivername="postgresql+psycopg")
    )
    source = f"kovo-contract-{uuid4().hex}"
    group_code = "001"
    now = datetime.now(UTC)
    body = FIXTURE.read_bytes()
    response = SourceResponse(
        source=source,
        request_key=SourceRequestKey(group_code, SourceEndpoint.SEASON_LIST),
        redacted_url="/stat/synthetic/season-list",
        requested_at=now,
        received_at=now,
        status_code=200,
        body_bytes=body,
    )
    service = MirrorIngestionService(
        RawSnapshotRepository(engine), MirrorFactRepository(engine), KovoParser()
    )

    first = service.ingest(response)
    repeated = service.ingest(response)

    with engine.connect() as connection:
        season_count = connection.execute(
            text("SELECT count(*) FROM mirror.seasons WHERE source=:source"),
            {"source": source},
        ).scalar_one()
        revision_count = connection.execute(
            text(
                """
                SELECT count(*) FROM mirror.season_identity_revisions sir
                JOIN mirror.seasons s ON s.id=sir.season_id
                WHERE s.source=:source
                """
            ),
            {"source": source},
        ).scalar_one()

    assert first.action is IngestAction.APPEND_REVISION
    assert first.fact_revisions > 0
    assert repeated.action is IngestAction.APPEND_RECEIPT_ONLY
    assert season_count == 2
    assert revision_count == 2
