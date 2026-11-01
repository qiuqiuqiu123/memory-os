"""Real PostgreSQL capture pipeline tests; review model is an explicit offline fixture."""

import copy
import json
import time
import uuid
from types import SimpleNamespace

import pytest
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


def test_review_is_proposal_until_approved_and_evidence_remains(core, model):
    uid = str(uuid.uuid4())
    body = batch()
    result = capture.ingest_batch(core, uid, body)
    job = claimed(core, result["review_job_ids"][0])
    capture.process_review(core, job)
    assert core.memories(uid, body["scope"]) == []
    review = capture.list_reviews(core, uid, body["scope"])[0]
    detail = capture.review_detail(core, uid, body["scope"], review["id"])
    candidate = detail["candidates"][0]
    assert candidate["status"] == "pending"
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


def test_candidate_approval_rolls_back_event_and_status_together(core, model, monkeypatch):
    uid = str(uuid.uuid4())
    result = capture.ingest_batch(core, uid, batch())
    capture.process_review(core, claimed(core, result["review_job_ids"][0]))
    review = capture.list_reviews(core, uid, "capture-test")[0]
    candidate = capture.review_detail(core, uid, "capture-test", review["id"])["candidates"][0]
    original_append = core.append

    def fail_after_event(*args, **kwargs):
        original_append(*args, **kwargs)
        raise RuntimeError("offline fixture simulates failure after event insert")

    monkeypatch.setattr(core, "append", fail_after_event)
    with pytest.raises(RuntimeError, match="offline fixture"):
        capture.approve_candidate(core, uid, "capture-test", candidate["id"])
    assert core.memories(uid, "capture-test") == []
    assert core.sync(uid, 0, 10)["events"] == []
    detail = capture.review_detail(core, uid, "capture-test", review["id"])
    assert detail["candidates"][0]["status"] == "pending"


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
