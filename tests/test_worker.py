"""Real PostgreSQL job transactions; Mem0 extraction is an explicit offline fixture."""

import uuid

from test_core import core
from memory_os import worker
from memory_os.integrations import Mem0Backend


def _job(core, user=None):
    user = user or str(uuid.uuid4())
    return core.enqueue(
        user,
        "worker-test",
        "extract",
        {"content": "offline input", "tags": ["preference"]},
        str(uuid.uuid4()),
    )


def _claim_specific(core, job_id):
    """Select our unique job, so concurrent integration suites cannot consume it."""
    with core.connection() as conn:
        return conn.execute(
            """UPDATE jobs SET status='running',attempts=attempts+1,
            lease_until=now()+interval '5 minutes' WHERE id=%s RETURNING *""",
            (job_id,),
        ).fetchone()


def _read(core, job_id):
    with core.connection() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()


def test_extraction_commits_facts_events_vector_jobs_and_completion_together(core, monkeypatch):
    job = _job(core)
    monkeypatch.setattr(
        Mem0Backend,
        "extract",
        lambda *args: [
            {"content": "Reply in Chinese", "operation": "ADD"},
            {"content": "Use Python examples", "operation": "ADD"},
        ],
    )
    claimed = _claim_specific(core, job["id"])
    worker.process(core, None, claimed)
    done = _read(core, job["id"])
    assert done["status"] == "completed" and done["lease_until"] is None
    memories = core.memories(job["user_id"], job["scope"])
    assert {m["id"] for m in memories} == set(done["result"]["memory_ids"])
    assert len(memories) == 2
    assert all(m["tags"] == ["preference"] for m in memories)
    assert len(core.sync(job["user_id"], 0, 100)["events"]) == 2
    with core.connection() as conn:
        vectors = conn.execute(
            "SELECT * FROM jobs WHERE user_id=%s AND scope=%s AND kind='vector'",
            (job["user_id"], job["scope"]),
        ).fetchall()
    assert len(vectors) == 2
    # A stale delivery cannot append a second set of facts after completion.
    worker.process(core, None, claimed)
    assert len(core.memories(job["user_id"], job["scope"])) == 2


def test_zero_facts_is_completed_without_fake_memories(core, monkeypatch):
    job = _job(core)
    monkeypatch.setattr(Mem0Backend, "extract", lambda *args: [])
    worker.process(core, None, _claim_specific(core, job["id"]))
    done = _read(core, job["id"])
    assert done["status"] == "completed"
    assert done["result"] == {"memory_ids": []}
    assert core.memories(job["user_id"], job["scope"]) == []
    assert core.sync(job["user_id"], 0, 100)["events"] == []


def test_provider_failure_is_durable_retry_then_failed_without_secret_echo(core, monkeypatch):
    job = _job(core)

    def offline(*args):
        raise RuntimeError("sensitive-provider-response-secret")

    monkeypatch.setattr(Mem0Backend, "extract", offline)
    # Only queue selection is scoped to our job. run_once's real retry SQL runs.
    monkeypatch.setattr(worker, "claim", lambda c: _claim_specific(c, job["id"]))
    assert worker.run_once(core, None)
    retry = _read(core, job["id"])
    assert retry["status"] == "retry" and retry["attempts"] == 1
    assert retry["lease_until"] is None and retry["available_at"] > retry["created_at"]
    assert "sensitive" not in retry["error"]
    with core.connection() as conn:
        conn.execute("UPDATE jobs SET attempts=4 WHERE id=%s", (job["id"],))
    assert worker.run_once(core, None)
    failed = _read(core, job["id"])
    assert failed["status"] == "failed" and failed["attempts"] == 5
    assert core.memories(job["user_id"], job["scope"]) == []


def test_attempt_fencing_rejects_late_old_extractor(core, monkeypatch):
    job = _job(core)
    old = _claim_specific(core, job["id"])

    def lease_stolen(*args):
        with core.connection() as conn:
            conn.execute("UPDATE jobs SET attempts=attempts+1 WHERE id=%s", (job["id"],))
        return [{"content": "stale extracted fact", "operation": "ADD"}]

    monkeypatch.setattr(Mem0Backend, "extract", lease_stolen)
    worker.process(core, None, old)
    active = _read(core, job["id"])
    assert active["status"] == "running" and active["attempts"] == 2
    assert core.memories(job["user_id"], job["scope"]) == []
    assert core.sync(job["user_id"], 0, 100)["events"] == []
    monkeypatch.setattr(
        Mem0Backend,
        "extract",
        lambda *args: [{"content": "fresh extracted fact", "operation": "ADD"}],
    )
    worker.process(core, None, active)
    assert _read(core, job["id"])["status"] == "completed"
    assert core.memories(job["user_id"], job["scope"])[0]["content"] == "fresh extracted fact"


def test_bad_extraction_result_rolls_back_all_facts_before_retry(core, monkeypatch):
    job = _job(core)
    monkeypatch.setattr(
        Mem0Backend,
        "extract",
        lambda *args: [
            {"content": "valid fact", "operation": "ADD"},
            {"content": "x" * 100001, "operation": "ADD"},
        ],
    )
    monkeypatch.setattr(worker, "claim", lambda c: _claim_specific(c, job["id"]))
    worker.run_once(core, None)
    assert _read(core, job["id"])["status"] == "retry"
    assert core.memories(job["user_id"], job["scope"]) == []
    assert core.sync(job["user_id"], 0, 100)["events"] == []
    with core.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM jobs WHERE user_id=%s AND kind='vector'",
                (job["user_id"],),
            ).fetchone()["n"]
            == 0
        )
