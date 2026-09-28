"""Completed polls are distinguishable from a merely running process."""

from datetime import UTC, datetime, timedelta

from vlytics.ops.heartbeat import heartbeat_is_fresh, record_heartbeat


def test_stalled_worker_heartbeat_expires(tmp_path):
    path = tmp_path / "heartbeat.json"
    now = datetime(2026, 9, 27, tzinfo=UTC)
    assert not heartbeat_is_fresh(path, now=now, max_age=timedelta(seconds=300))
    record_heartbeat(path, now=now, processed=0)
    assert heartbeat_is_fresh(path, now=now, max_age=timedelta(seconds=300))
    assert not heartbeat_is_fresh(
        path, now=now + timedelta(seconds=301), max_age=timedelta(seconds=300)
    )
    assert not heartbeat_is_fresh(
        path, now=now - timedelta(seconds=1), max_age=timedelta(seconds=300)
    )
    path.write_text("invalid")
    assert not heartbeat_is_fresh(path, now=now, max_age=timedelta(seconds=300))
