"""Real PostgreSQL capture pipeline tests; review model is an explicit offline fixture."""

import copy
import json
import time
import uuid
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb
from test_core import core as pg_core  # noqa: F401

from memory_os import capture
from memory_os.config import Settings
from memory_os.core import Conflict, InvalidEvent


@pytest.fixture
def core(pg_core):  # noqa: F811
    capture.init_schema(pg_core)
    return pg_core


def batch(count=1, *, session=None, batch_id=None, start=0, complete=True):
    return {
        "scope": "capture-test",
        "source_session_id": session or str(uuid.uuid4()),
        "batch_id": batch_id or str(uuid.uuid4()),
        "turn_complete": complete,
        "events": [
            {
                "source_id": f"e{i}",
                "type": "tool_result",
                "timestamp": "2026-11-01T01:00:00Z",
                "body": {"text": f"Execution result {i}"},
            }
            for i in range(start, start + count)
        ],
    }


def claimed(core, job_id):
    with core.connection() as conn:
        return conn.execute(
            "UPDATE jobs SET status='running',attempts=attempts+1 WHERE id=%s RETURNING *",
            (job_id,),
        ).fetchone()


def fixture_result():
    return {
        "summary": "A tool execution was recorded.",
        "findings": [{"text": "Result was available.", "evidence_ids": ["e0"]}],
        "optimizations": [{"text": "Check the result before retrying.", "evidence_ids": ["e0"]}],
        "memory_candidates": [
            {
                "content": "Check tool output before retrying.",
                "tags": ["practice"],
                "evidence_ids": ["e0"],
            }
        ],
        "evidence_ids": ["e0"],
    }


@pytest.fixture
def model(monkeypatch):
    from memory_os import config, runtime

    active = Settings(_env_file=None, llm_api_key="offline-provider-key", llm_model="offline-model")
    monkeypatch.setattr(config, "settings", lambda: active)
    monkeypatch.setattr(runtime, "resolve_settings", lambda base, uid: active)
    state = {"result": fixture_result(), "calls": [], "callback": None, "finish_reason": "stop"}

    class OfflineOpenAI:
        def __init__(self, **kwargs):
            assert kwargs["timeout"] < 300 and kwargs["max_retries"] == 0
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def create(self, **kwargs):
            state["calls"].append(kwargs)
            if state["callback"]:
                state["callback"]()
            content = (
                state["result"] if isinstance(state["result"], str) else json.dumps(state["result"])
            )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=content),
                        finish_reason=state["finish_reason"],
                    )
                ],
                usage=None,
            )

    monkeypatch.setattr(capture, "OpenAI", OfflineOpenAI)
    return state


def test_ingest_redaction_idempotency_conflicts_and_isolation(core):
    uid = str(uuid.uuid4())
    body = batch()
    body["events"][0]["body"] = {
        "api_key": "raw-secret",
        "text": 'api_key="quoted-secret" Bearer bearer-secret',
        "nested": {"password": "hidden"},
    }
    result = capture.ingest_batch(core, uid, body)
    detail = capture.session_detail(core, uid, body["scope"], result["session_id"])
    encoded = json.dumps(detail, default=str)
    assert all(
        secret not in encoded
        for secret in ("raw-secret", "quoted-secret", "bearer-secret", "hidden")
    )
    assert "[REDACTED]" in encoded
    assert capture.ingest_batch(core, uid, body)["accepted"] == 0
    changed = copy.deepcopy(body)
    changed["events"][0]["body"]["api_key"] = "changed-secret"
    with pytest.raises(Conflict):
        capture.ingest_batch(core, uid, changed)
    changed["batch_id"] = str(uuid.uuid4())
    with pytest.raises(Conflict):
        capture.ingest_batch(core, uid, changed)
    assert (
        capture.session_detail(core, str(uuid.uuid4()), body["scope"], result["session_id"]) is None
    )
    assert capture.session_detail(core, uid, "other-scope", result["session_id"]) is None
    assert capture.list_sessions(core, uid, body["scope"])[0]["event_count"] == 1
    assert core.memories(uid, body["scope"]) == []


def test_incremental_windows_only_review_new_events(core):
    uid = str(uuid.uuid4())
    first = batch(99, complete=False)
    assert capture.ingest_batch(core, uid, first)["review_job_ids"] == []
    second = batch(1, session=first["source_session_id"], start=99, complete=False)
    result = capture.ingest_batch(core, uid, second)
    assert len(result["review_job_ids"]) == 1

    third = batch(3, session=first["source_session_id"], start=100)
    last = capture.ingest_batch(core, uid, third)
    assert len(last["review_job_ids"]) == 1
    with core.connection() as conn:
        rows = conn.execute(
            "SELECT payload FROM jobs WHERE user_id=%s AND kind='capture_review' ORDER BY created_at",
            (uid,),
        ).fetchall()
    assert [(r["payload"]["start_seq"], r["payload"]["end_seq"]) for r in rows] == [
        (1, 100),
        (101, 103),
    ]
    empty = batch(0, session=first["source_session_id"])
    assert capture.ingest_batch(core, uid, empty)["review_job_ids"] == []


def test_redaction_handles_stringified_json_env_headers_and_private_key():
    value = {
        "env": {
            "OPENAI_API_KEY": "env-secret",
            "AWS_ACCESS_KEY_ID": "aws-secret",
            "token": "token-secret",
        },
        "headers": {"Authorization": "header-secret", "Cookie": "cookie-secret"},
        "text": '{"client_secret":"json-secret", "api_key":"quoted-secret"}',
        "pem": "-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----",
    }
    cleaned = json.dumps(capture.redact(value))
    for secret in (
        "env-secret",
        "aws-secret",
        "token-secret",
        "header-secret",
        "cookie-secret",
        "json-secret",
        "quoted-secret",
        "private-material",
    ):
        assert secret not in cleaned


def test_review_validation_requires_citations_and_validates_empty_candidates():
    valid = fixture_result()
    valid["memory_candidates"] = []
    assert capture._validate_review(valid, {"e0"})["memory_candidates"] == []
    valid["findings"][0]["evidence_ids"] = ["outside-window"]
    with pytest.raises(ValueError, match="evidence"):
        capture._validate_review(valid, {"e0"})


@pytest.mark.parametrize("prefix", ["中文日志" * 10000, "a" * 40000])
def test_redaction_long_logs_avoid_quadratic_backtracking(prefix):
    # A secret after the long section must still be masked before truncation.
    value = prefix + '\n{"client_secret":"fixture-secret", "safe":"ordinary"}'
    started = time.perf_counter()
    cleaned = capture.redact(value)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.2
    assert cleaned.endswith("[truncated]")
    assert "fixture-secret" not in cleaned
    # Validate the assignment callback directly, since redact truncates long text.
    assert (
        capture.redact('{"client_secret":"fixture-secret", "safe":"ordinary"}')
        == '{"client_secret":"[REDACTED]", "safe":"ordinary"}'
    )


def test_review_auto_saves_with_evidence_and_retry_is_idempotent(core, model):
    uid = str(uuid.uuid4())
    body = batch()
    result = capture.ingest_batch(core, uid, body)
    job = claimed(core, result["review_job_ids"][0])
    capture.process_review(core, job)
    assert len(core.memories(uid, body["scope"])) == 1
    review = capture.list_reviews(core, uid, body["scope"])[0]
    detail = capture.review_detail(core, uid, body["scope"], review["id"])
    candidate = detail["candidates"][0]
    assert candidate["status"] == "auto_saved"
    assert candidate["evidence_ids"] == ["e0"]
    assert (
        capture.approve_candidate(core, str(uuid.uuid4()), body["scope"], candidate["id"]) is None
    )
    approved = capture.approve_candidate(core, uid, body["scope"], candidate["id"])
    assert capture.approve_candidate(core, uid, body["scope"], candidate["id"])["duplicate"]
    assert len(core.memories(uid, body["scope"])) == 1
    event = core.sync(uid, 0, 10)["events"][0]
    assert event["metadata"]["capture_review_id"] == str(review["id"])
    assert event["metadata"]["evidence_ids"] == ["e0"]
    assert event["entity_id"] == approved["memory_id"]
    capture.process_review(core, job)
    assert len(model["calls"]) == 1  # no repeated fee for completed work
    assert "untrusted" in model["calls"][0]["messages"][0]["content"]


@pytest.mark.parametrize(
    "bad",
    [
        "not JSON",
        {},
        {"summary": "No evidence"},
        {
            "summary": "",
            "findings": [],
            "optimizations": [],
            "memory_candidates": [],
            "evidence_ids": ["e0"],
        },
    ],
)
def test_invalid_model_output_never_commits_success(core, model, bad):
    uid = str(uuid.uuid4())
    result = capture.ingest_batch(core, uid, batch())
    job = claimed(core, result["review_job_ids"][0])
    model["result"] = bad
    with pytest.raises(RuntimeError, match="Capture review failed"):
        capture.process_review(core, job)
    assert capture.list_reviews(core, uid, "capture-test") == []
    with core.connection() as conn:
        assert (
            conn.execute("SELECT status FROM jobs WHERE id=%s", (job["id"],)).fetchone()["status"]
            == "running"
        )


def test_out_of_window_citation_and_attempt_fence(core, model):
    uid = str(uuid.uuid4())
    result = capture.ingest_batch(core, uid, batch())
    job = claimed(core, result["review_job_ids"][0])
    model["result"]["memory_candidates"][0]["evidence_ids"] = ["invented-id"]
    with pytest.raises(RuntimeError):
        capture.process_review(core, job)
    model["result"] = fixture_result()

    def steal():
        with core.connection() as conn:
            conn.execute("UPDATE jobs SET attempts=attempts+1 WHERE id=%s", (job["id"],))

    model["callback"] = steal
    capture.process_review(core, job)
    assert capture.list_reviews(core, uid, "capture-test") == []
    assert core.memories(uid, "capture-test") == []


def test_capture_type_allowlist_large_cjk_and_private_keys(core):
    uid = str(uuid.uuid4())
    bad = batch()
    bad["events"][0]["type"] = "reasoning"
    with pytest.raises(InvalidEvent):
        capture.ingest_batch(core, uid, bad)
    body = batch()
    body["events"][0]["body"] = {
        "text": "中文日志" * 10000,
        "private": "-----BEGIN RSA PRIVATE KEY-----\nSECRET\n-----END RSA PRIVATE KEY-----",
    }
    result = capture.ingest_batch(core, uid, body)
    event = capture.session_detail(core, uid, body["scope"], result["session_id"])["events"][0]
    assert "SECRET" not in json.dumps(event)
    assert event["body"]["truncated"]
    assert len(result["review_job_ids"]) == 1


def test_review_auto_save_rolls_back_review_event_and_job_together(core, model, monkeypatch):
    uid = str(uuid.uuid4())
    result = capture.ingest_batch(core, uid, batch())
    job = claimed(core, result["review_job_ids"][0])
    original_append = core.append

    def fail_after_event(*args, **kwargs):
        original_append(*args, **kwargs)
        raise RuntimeError("offline fixture simulates failure after event insert")

    monkeypatch.setattr(core, "append", fail_after_event)
    with pytest.raises(RuntimeError, match="offline fixture"):
        capture.process_review(core, job)
    assert core.memories(uid, "capture-test") == []
    assert core.sync(uid, 0, 10)["events"] == []
    assert capture.list_reviews(core, uid, "capture-test") == []
    with core.connection() as conn:
        assert (
            conn.execute("SELECT status FROM jobs WHERE id=%s", (job["id"],)).fetchone()["status"]
            == "running"
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM capture_candidates WHERE user_id=%s", (uid,)
            ).fetchone()["n"]
            == 0
        )


@pytest.mark.parametrize(
    "base_url,model,budget_key,thinking",
    [
        ("https://api.deepseek.com", "deepseek-flash", "max_tokens", True),
        ("https://api.deepseek.com/v1", "deepseek-flash", "max_tokens", True),
        ("https://gateway.example/v1", "deepseek-flash", "max_tokens", False),
        ("https://api.deepseek.com.gateway.example/v1", "deepseek-flash", "max_tokens", False),
        ("https://api.openai.com/v1", "gpt-5-mini", "max_completion_tokens", False),
    ],
)
def test_review_provider_parameters_budget_and_vendor_boundary(
    base_url, model, budget_key, thinking
):
    active = Settings(_env_file=None, llm_base_url=base_url, llm_model=model)
    parameters = capture._review_provider_parameters(active)
    assert parameters[budget_key] == 6000
    assert ("extra_body" in parameters) is thinking
    if thinking:
        assert parameters["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_incomplete_model_output_never_commits_even_if_json_is_parseable(core, model, reason):
    uid = str(uuid.uuid4())
    result = capture.ingest_batch(core, uid, batch())
    job = claimed(core, result["review_job_ids"][0])
    model["finish_reason"] = reason
    with pytest.raises(RuntimeError, match="Capture review failed"):
        capture.process_review(core, job)
    assert len(model["calls"]) == 1
    assert model["calls"][0]["max_tokens"] == 6000
    assert capture.list_reviews(core, uid, "capture-test") == []


def legacy_candidate(core, uid, scope="capture-test", content="A confirmed legacy fact"):
    body = batch(complete=False)
    body["scope"] = scope
    session = capture.ingest_batch(core, uid, body)
    rid, cid = uuid.uuid4(), uuid.uuid4()
    with core.connection() as conn:
        conn.execute(
            "INSERT INTO capture_reviews(id,session_id,user_id,scope,start_seq,end_seq,result,model) "
            "VALUES(%s,%s,%s,%s,1,1,%s,'offline-fixture')",
            (rid, session["session_id"], uid, scope, Jsonb(fixture_result())),
        )
        conn.execute(
            "INSERT INTO capture_candidates(id,review_id,user_id,scope,content,tags,evidence_ids) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (cid, rid, uid, scope, content, Jsonb(["practice"]), Jsonb(["e0"])),
        )
    return cid


def reviewed_candidate(core, model, uid, content=None, scope="capture-test"):
    if content is not None:
        model["result"]["memory_candidates"][0]["content"] = content
    body = batch()
    body["scope"] = scope
    result = capture.ingest_batch(core, uid, body)
    capture.process_review(core, claimed(core, result["review_job_ids"][0]))
    # Retrieve exact review by newly created session, not timestamp ordering.
    review = next(
        r
        for r in capture.list_reviews(core, uid, scope)
        if str(r["session_id"]) == result["session_id"]
    )
    return capture.review_detail(core, uid, scope, review["id"])["candidates"][0]


def test_automatic_dedup_and_reject_blocks_future_reappearance(core, model):
    uid = str(uuid.uuid4())
    first = reviewed_candidate(core, model, uid, "Check tool output before retrying.")
    duplicate = reviewed_candidate(core, model, uid, "Ｃheck  tool\noutput before retrying.")
    assert first["memory_id"] == duplicate["memory_id"]
    assert len(core.memories(uid, "capture-test")) == 1
    assert len(core.sync(uid, 0, 100)["events"]) == 1
    assert capture.reject_candidate(core, str(uuid.uuid4()), "capture-test", first["id"]) is None
    assert capture.reject_candidate(core, uid, "different-scope", first["id"]) is None
    rejected = capture.reject_candidate(core, uid, "capture-test", first["id"])
    assert rejected["status"] == "rejected"
    assert capture.reject_candidate(core, uid, "capture-test", first["id"])["duplicate"]
    assert core.memories(uid, "capture-test") == []
    later = reviewed_candidate(core, model, uid, "Check tool output before retrying.")
    assert later["status"] == "rejected"
    assert core.memories(uid, "capture-test") == []
    assert len(core.sync(uid, 0, 100)["events"]) == 2  # create + delete, never resurrect
    with pytest.raises(Conflict, match="rejected"):
        capture.approve_candidate(core, uid, "capture-test", later["id"])
    other_user = str(uuid.uuid4())
    assert reviewed_candidate(core, model, other_user)["status"] == "auto_saved"
    assert reviewed_candidate(core, model, uid, scope="another-project")["status"] == "auto_saved"


def test_normal_memory_delete_rejects_links_and_is_not_revived(core, model):
    uid = str(uuid.uuid4())
    item = reviewed_candidate(core, model, uid)
    core.append(
        uid,
        "capture-test",
        [
            {
                "event_id": str(uuid.uuid4()),
                "event_type": "memory.deleted",
                "entity_type": "memory",
                "entity_id": item["memory_id"],
                "payload": {"reason": "ordinary REST/MCP deletion"},
            }
        ],
    )
    assert capture.reject_candidate(core, uid, "capture-test", item["id"])["duplicate"]
    assert reviewed_candidate(core, model, uid)["status"] == "rejected"
    assert core.memories(uid, "capture-test") == []


def test_backfill_is_bounded_scoped_and_deduplicates_legacy_candidates(core):
    uid, other_uid = str(uuid.uuid4()), str(uuid.uuid4())
    legacy_candidate(core, uid)
    legacy_candidate(core, uid, content="A  confirmed legacy fact")
    legacy_candidate(core, uid, scope="different-scope")
    legacy_candidate(core, other_uid)
    first = capture.backfill_candidates(core, limit=1, uid=uid, scope="capture-test")
    assert first == {"processed": 1, "saved": 1, "created": 1, "rejected": 0}
    second = capture.backfill_candidates(core, limit=1, uid=uid, scope="capture-test")
    assert second == {"processed": 1, "saved": 1, "created": 0, "rejected": 0}
    assert capture.backfill_candidates(core, uid=uid, scope="capture-test")["processed"] == 0
    assert len(core.memories(uid, "capture-test")) == 1
    assert core.memories(uid, "different-scope") == []
    assert core.memories(other_uid, "capture-test") == []


def test_legacy_manual_approval_and_rejection_before_backfill(core):
    uid = str(uuid.uuid4())
    cid = legacy_candidate(core, uid)
    approved = capture.approve_candidate(core, uid, "capture-test", cid)
    assert approved["status"] == "approved"
    assert capture.approve_candidate(core, uid, "capture-test", cid)["duplicate"]
    rejected_cid = legacy_candidate(core, uid, content="A fact user does not want saved")
    assert capture.reject_candidate(core, uid, "capture-test", rejected_cid)["status"] == "rejected"
    assert capture.backfill_candidates(core, uid=uid)["processed"] == 0
    assert len(core.memories(uid, "capture-test")) == 1


def test_legacy_approval_rolls_back_status_and_event_together(core, monkeypatch):
    uid = str(uuid.uuid4())
    cid = legacy_candidate(core, uid)
    original_append = core.append

    def fail_after_event(*args, **kwargs):
        original_append(*args, **kwargs)
        raise RuntimeError("offline fixture simulates approval failure")

    monkeypatch.setattr(core, "append", fail_after_event)
    with pytest.raises(RuntimeError, match="approval failure"):
        capture.approve_candidate(core, uid, "capture-test", cid)
    assert core.memories(uid, "capture-test") == []
    with core.connection() as conn:
        assert (
            conn.execute("SELECT status FROM capture_candidates WHERE id=%s", (cid,)).fetchone()[
                "status"
            ]
            == "pending"
        )


def test_rejection_rolls_back_deleted_event_and_candidate_together(core, model, monkeypatch):
    uid = str(uuid.uuid4())
    candidate = reviewed_candidate(core, model, uid)
    original_append = core.append

    def fail_after_event(*args, **kwargs):
        original_append(*args, **kwargs)
        raise RuntimeError("offline fixture simulates reject failure")

    monkeypatch.setattr(core, "append", fail_after_event)
    with pytest.raises(RuntimeError, match="reject failure"):
        capture.reject_candidate(core, uid, "capture-test", candidate["id"])
    assert len(core.memories(uid, "capture-test")) == 1
    with core.connection() as conn:
        assert (
            conn.execute(
                "SELECT status FROM capture_candidates WHERE id=%s", (candidate["id"],)
            ).fetchone()["status"]
            == "auto_saved"
        )


def test_backfill_does_not_resurrect_legacy_deleted_memory_id(core):
    uid = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    core.append(
        uid,
        "capture-test",
        [
            {
                "event_id": str(uuid.uuid4()),
                "event_type": "memory.created",
                "entity_type": "memory",
                "entity_id": mid,
                "payload": {"content": "Legacy confirmed fact", "tags": []},
            }
        ],
    )
    core.append(
        uid,
        "capture-test",
        [
            {
                "event_id": str(uuid.uuid4()),
                "event_type": "memory.deleted",
                "entity_type": "memory",
                "entity_id": mid,
                "payload": {"reason": "user removed legacy record"},
            }
        ],
    )
    cid = legacy_candidate(core, uid, content="Legacy  confirmed fact")
    assert capture.backfill_candidates(core, uid=uid) == {
        "processed": 1,
        "saved": 0,
        "created": 0,
        "rejected": 1,
    }
    assert core.memories(uid, "capture-test") == []
    assert capture.reject_candidate(core, uid, "capture-test", cid)["duplicate"]
    assert len(core.sync(uid, 0, 100)["events"]) == 2


def test_concurrent_promotions_share_one_memory(core):
    from concurrent.futures import ThreadPoolExecutor

    uid = str(uuid.uuid4())
    ids = [legacy_candidate(core, uid) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda cid: capture.approve_candidate(core, uid, "capture-test", cid), ids)
        )
    assert results[0]["memory_id"] == results[1]["memory_id"]
    assert sum(bool(r["created"]) for r in results) == 1
    assert len(core.memories(uid, "capture-test")) == 1
    assert len(core.sync(uid, 0, 100)["events"]) == 1


@pytest.mark.parametrize("legacy_id", [False, True])
def test_user_edit_is_preserved_when_later_review_proposes_old_content(core, model, legacy_id):
    uid, scope = str(uuid.uuid4()), "capture-test"
    original_content, edited_content = (
        "The project uses Python 3.11.",
        "The project now uses Python 3.12.",
    )
    if legacy_id:
        cid = legacy_candidate(core, uid, content=original_content)
        mid = str(uuid.uuid5(cid, "memory"))
        core.append(
            uid,
            scope,
            [
                {
                    "event_id": str(uuid.uuid4()),
                    "event_type": "memory.created",
                    "entity_type": "memory",
                    "entity_id": mid,
                    "payload": {"content": original_content, "tags": []},
                }
            ],
        )
        with core.connection() as conn:
            conn.execute(
                "UPDATE capture_candidates SET status='approved',memory_id=%s WHERE id=%s",
                (mid, cid),
            )
        original_status = "approved"
    else:
        item = reviewed_candidate(core, model, uid, original_content)
        cid, mid, original_status = item["id"], item["memory_id"], "auto_saved"
    core.append(
        uid,
        scope,
        [
            {
                "event_id": str(uuid.uuid4()),
                "event_type": "memory.updated",
                "entity_type": "memory",
                "entity_id": mid,
                "payload": {"new_content": edited_content, "reason": "user corrected fact"},
            }
        ],
    )
    later = reviewed_candidate(core, model, uid, original_content)
    assert later["status"] == "rejected"
    assert later["memory_id"] is None  # do not link rejection to the corrected active fact
    memories = core.memories(uid, scope)
    assert len(memories) == 1 and memories[0]["id"] == mid
    assert memories[0]["content"] == edited_content
    with core.connection() as conn:
        assert (
            conn.execute("SELECT status FROM capture_candidates WHERE id=%s", (cid,)).fetchone()[
                "status"
            ]
            == original_status
        )
        assert (
            conn.execute(
                "SELECT status FROM jobs WHERE user_id=%s AND kind='capture_review' ORDER BY created_at DESC LIMIT 1",
                (uid,),
            ).fetchone()["status"]
            == "completed"
        )
    core.append(
        uid,
        scope,
        [
            {
                "event_id": str(uuid.uuid4()),
                "event_type": "memory.deleted",
                "entity_type": "memory",
                "entity_id": mid,
                "payload": {"reason": "user removed corrected fact"},
            }
        ],
    )
    assert reviewed_candidate(core, model, uid, original_content)["status"] == "rejected"
    assert reviewed_candidate(core, model, uid, edited_content)["status"] == "rejected"
    assert core.memories(uid, scope) == []


@pytest.mark.parametrize("legacy_id", [False, True])
def test_rejecting_stale_candidate_cannot_delete_user_edited_memory(core, model, legacy_id):
    uid, scope = str(uuid.uuid4()), "capture-test"
    original_content, edited_content = "Project fact A", "User corrected fact B"
    if legacy_id:
        cid = legacy_candidate(core, uid, content=original_content)
        mid, original_status = str(uuid.uuid5(cid, "memory")), "approved"
        core.append(
            uid,
            scope,
            [
                {
                    "event_id": str(uuid.uuid4()),
                    "event_type": "memory.created",
                    "entity_type": "memory",
                    "entity_id": mid,
                    "payload": {"content": original_content, "tags": []},
                }
            ],
        )
        with core.connection() as conn:
            conn.execute(
                "UPDATE capture_candidates SET status='approved',memory_id=%s WHERE id=%s",
                (mid, cid),
            )
    else:
        item = reviewed_candidate(core, model, uid, original_content)
        cid, mid, original_status = item["id"], item["memory_id"], "auto_saved"
    core.append(
        uid,
        scope,
        [
            {
                "event_id": str(uuid.uuid4()),
                "event_type": "memory.updated",
                "entity_type": "memory",
                "entity_id": mid,
                "payload": {"new_content": edited_content, "reason": "user correction"},
            }
        ],
    )
    before_events = core.sync(uid, 0, 100)["events"]
    with pytest.raises(Conflict, match="Memory was edited"):
        capture.reject_candidate(core, uid, scope, cid)
    memories = core.memories(uid, scope)
    assert len(memories) == 1 and memories[0]["content"] == edited_content
    assert core.sync(uid, 0, 100)["events"] == before_events
    with core.connection() as conn:
        assert (
            conn.execute("SELECT status FROM capture_candidates WHERE id=%s", (cid,)).fetchone()[
                "status"
            ]
            == original_status
        )
