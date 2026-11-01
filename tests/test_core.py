"""Real PostgreSQL tests; MODEL calls are not exercised by this suite."""

import concurrent.futures
import os
import uuid

import pytest
from memory_os.core import Core, Conflict, InvalidEvent


@pytest.fixture
def core():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for real PostgreSQL integration tests")
    c = Core(url)
    c.initialize()
    return c


def event(kind="memory.created", entity=None, **payload):
    return dict(
        event_id=str(uuid.uuid4()),
        event_type=kind,
        entity_type="memory",
        entity_id=entity or str(uuid.uuid4()),
        payload=payload,
    )


def test_idempotency_and_collision(core):
    user = str(uuid.uuid4())
    e = event(content="中文偏好", tags=["preference"])
    first = core.append(user, "project", [e])
    assert first["accepted"] == 1
    assert core.append(user, "project", [e])["accepted"] == 0
    assert len(core.memories(user, "project")) == 1
    changed = dict(e, payload={"content": "other"})
    with pytest.raises(Conflict):
        core.append(user, "project", [changed])
    assert core.memories(str(uuid.uuid4()), "project") == []


def test_concurrent_commits_and_sync(core):
    user = str(uuid.uuid4())

    def put(i):
        return core.append(user, "a", [event(content=str(i))])

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as p:
        results = list(p.map(put, range(100)))
    versions = sorted(r["last_version"] for r in results)
    assert len(set(versions)) == 100
    cursor = 0
    seen = []
    while True:
        page = core.sync(user, cursor, 13)
        seen.extend(e["id"] for e in page["events"])
        cursor = page["next_version"]
        if not page["has_more"]:
            break
    assert seen == versions


def test_rebuild_history_scope_and_delete(core):
    user = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    core.append(user, "a", [event(entity=mid, content="one")])
    core.append(user, "a", [event("memory.updated", mid, new_content="two", reason="edit")])
    assert core.memories(user, "a")[0]["content"] == "two"
    assert core.memories(user, "b") == []
    before = core.memories(user, "a")
    core.rebuild(user)
    assert core.memories(user, "a") == before
    core.append(user, "a", [event("memory.deleted", mid, reason="test")])
    assert core.memories(user, "a") == []
    core.rebuild(user)
    assert core.memories(user, "a") == []
    assert len(core.sync(user, 0, 100)["events"]) == 3


def test_batch_atomicity(core):
    user = str(uuid.uuid4())
    with pytest.raises(InvalidEvent):
        core.append(
            user,
            "a",
            [event(content="valid"), event("memory.updated", new_content="missing")],
        )
    assert core.sync(user, 0, 100)["events"] == []


def test_skill_stats_trace_and_errors(core):
    user = str(uuid.uuid4())
    tid = str(uuid.uuid4())
    events = [dict(event("trace.started", tid, task="task"), entity_type="trace")]
    for i in range(10):
        events.append(
            dict(
                event(
                    "skill.invoked",
                    f"span-{i}",
                    skill_name="test",
                    trace_id=tid,
                    status="success" if i < 8 else "error",
                    duration_ms=100,
                    tokens=20,
                    error_type=None if i < 8 else "timeout",
                ),
                entity_type="skill",
            )
        )
    core.append(user, "a", events)
    stats = core.analysis(user, "a")["skills"][0]
    assert stats["success_rate"] == 0.8 and stats["avg_tokens"] == 20
    assert stats["top_errors"][0]["error_type"] == "timeout"
    core.rebuild(user)
    assert core.analysis(user, "a")["skills"][0] == stats


def test_lww_and_old_content_captured(core):
    user = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    core.append(user, "a", [event(entity=mid, content="first")])
    core.append(user, "a", [event("memory.updated", mid, new_content="second")])
    events = core.sync(user, 0, 100)["events"]
    assert events[1]["payload"]["old_content"] == "first"
    core.append(
        user,
        "a",
        [event("memory.updated", mid, new_content="third", old_content="stale")],
    )
    assert core.memories(user, "a")[0]["content"] == "third"


def test_tenant_idempotency(core):
    e = event(content="private")
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    core.append(a, "a", [e])
    core.append(b, "a", [e])
    assert len(core.memories(b, "a")) == 1


def test_append_only_database_permissions_guard(core):
    with core.connection() as conn:
        with pytest.raises(Exception, match="append-only"):
            conn.execute("DELETE FROM events WHERE FALSE")


def test_semantic_import_reversions_and_running_exclusion(core):
    user = str(uuid.uuid4())
    sid = str(uuid.uuid4())

    def skill(status):
        return dict(
            event("skill.invoked", sid, skill_name="test", status=status, error_type="timeout"),
            entity_type="skill",
            metadata={"source": "langfuse", "source_trace_id": "t"},
        )

    assert core.import_semantic(user, "a", [skill("running")])["accepted"] == 1
    assert core.analysis(user, "a")["skills"] == []
    assert core.import_semantic(user, "a", [skill("success")])["accepted"] == 1
    assert core.import_semantic(user, "a", [skill("error")])["accepted"] == 1
    assert core.import_semantic(user, "a", [skill("success")])["accepted"] == 1
    assert core.import_semantic(user, "a", [skill("success")])["accepted"] == 0
    assert core.analysis(user, "a")["skills"][0]["success_rate"] == 1
    core.rebuild(user)
    assert core.analysis(user, "a")["skills"][0]["calls"] == 1
