"""Injection is deterministic; no model calls or invented semantic results."""

import uuid
import pytest
from memory_os.core import Core
from memory_os.service import Service, new_event, ENCODING


@pytest.fixture
def service(core):
    return Service(core, None)


# Share the PostgreSQL fixture without replacing the database.
from test_core import core


def test_injection_budget_and_skill_isolation(service):
    uid = str(uuid.uuid4())
    scope = "a"
    service.remember(uid, scope, "请使用中文", ["preference"], False)
    service.remember(uid, scope, "每次部署前先核验健康检查", ["skill:deploy"], False)
    service.remember(uid, scope, "x " * 2000, ["skill:deploy"], False)
    result = service.inject(uid, scope, "skill.invoked", skill_name="deploy", budget=100)
    assert result["token_count"] == len(ENCODING.encode(result["content"])) <= 100
    assert "中文" not in result["content"] and "健康检查" in result["content"]
    assert result["truncated"]
    events = service.core.sync(uid, 0, 100)["events"]
    assert events[-1]["event_type"] == "memory.injected"
    service.core.rebuild(uid)
    assert service.inject(uid, scope, "session.start")["content"].endswith("请使用中文\n")


def test_remember_durable_idempotency(service):
    uid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    first = service.remember(uid, "a", "fact", infer=False, request_id=rid)
    again = service.remember(uid, "a", "fact", infer=False, request_id=rid)
    assert first["memory_id"] == again["memory_id"] and again["accepted"] == 0
    j1 = service.remember(uid, "a", "extract me", request_id=rid)
    j2 = service.remember(uid, "a", "extract me", request_id=rid)
    assert j1["job_id"] == j2["job_id"]


def test_evaluation_does_not_claim_causality(service):
    uid = str(uuid.uuid4())

    def calls(count):
        service.core.append(
            uid,
            "a",
            [
                new_event(
                    "skill.invoked",
                    str(uuid.uuid4()),
                    {"skill_name": "test", "status": "error", "error_type": "timeout"},
                    "skill",
                )
                for _ in range(count)
            ],
        )

    calls(2)
    rec = service.recommend(uid, "a", "test")
    service.inject(uid, "a", "skill.invoked", skill_name="test")
    calls(2)
    stats = service.core.analysis(uid, "a")["skills"][0]
    assert stats["evaluation"]["conclusion"] == "insufficient_evidence"
    assert (
        rec["memory_id"]
        in service.inject(uid, "a", "skill.invoked", skill_name="test")["memory_ids"]
    )


def test_literal_token_markers_are_treated_as_user_text(service):
    uid = str(uuid.uuid4())
    service.remember(uid, "a", "literal <|endoftext|> marker", ["preference"], False)
    result = service.inject(uid, "a", "session.start")
    assert "<|endoftext|>" in result["content"]
    assert result["token_count"] <= 500
