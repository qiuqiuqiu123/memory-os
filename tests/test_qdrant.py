"""Real Qdrant + PostgreSQL; 3D vectors are offline fixtures, NOT model quality tests."""

import os
import uuid

import pytest

from test_core import core
from memory_os.config import Settings
from memory_os.semantic import Semantic
from memory_os.service import Service, new_event


@pytest.fixture
def semantic():
    url = os.getenv("QDRANT_TEST_URL")
    if not url:
        pytest.skip("QDRANT_TEST_URL is required for real Qdrant tests")
    # Unique collection, user IDs and scopes prevent interference with other suites.
    value = Semantic(
        Settings(
            qdrant_url=url,
            qdrant_collection="test_" + uuid.uuid4().hex,
            embedding_dimensions=3,
        )
    )
    value.embed = lambda text: [1.0, 0.0, 0.0] if "Chinese" in text else [0.0, 1.0, 0.0]
    yield value
    # Preserve the temporary collection; deletion requires explicit human approval.
    value.q.close()


def _memory(user, scope, mid=None, **changes):
    result = {
        "user_id": user,
        "scope": scope,
        "id": mid or str(uuid.uuid4()),
        "content": "Chinese replies",
        "version": 1,
        "tags": ["preference"],
        "deleted": False,
    }
    return {**result, **changes}


def test_real_vector_search_tenant_scope_tags_and_soft_delete(semantic):
    user, other = str(uuid.uuid4()), str(uuid.uuid4())
    a = _memory(user, "a")
    b = _memory(user, "b")
    c = _memory(other, "a")
    d = _memory(user, "a", content="Python examples", tags=["knowledge"])
    for item in (a, b, c, d):
        semantic.index(item)
    hits = semantic.search(user, "a", "Chinese", 10)
    assert {hit.payload["memory_id"] for hit in hits} == {a["id"], d["id"]}
    assert hits[0].payload["memory_id"] == a["id"]
    tagged = semantic.search(user, "a", "Chinese", 10, ["preference"])
    assert [hit.payload["memory_id"] for hit in tagged] == [a["id"]]
    semantic.index({**a, "deleted": True, "version": 2})
    assert semantic.search(user, "a", "Chinese", 10, ["preference"]) == []
    assert semantic.search(other, "a", "Chinese", 10)[0].payload["memory_id"] == c["id"]


def test_service_filters_stale_vectors_until_current_version_indexed(core, semantic):
    user = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    scope = "qdrant-test"
    service = Service(core, semantic)
    core.append(
        user,
        scope,
        [
            new_event(
                "memory.created",
                mid,
                {"content": "Chinese replies", "tags": ["preference"]},
            )
        ],
    )
    original = core.memory(user, scope, mid)
    semantic.index(original)
    assert service.recall(user, scope, "Chinese")["items"][0]["id"] == mid
    core.append(
        user,
        scope,
        [new_event("memory.updated", mid, {"new_content": "Python examples"})],
    )
    assert service.recall(user, scope, "Chinese")["items"] == []
    semantic.index(core.memory(user, scope, mid))
    assert service.recall(user, scope, "Python")["items"][0]["content"] == "Python examples"
    core.append(user, scope, [new_event("memory.deleted", mid, {"reason": "test"})])
    # DB tombstone suppresses stale Qdrant points before the delete job runs.
    assert service.recall(user, scope, "Python")["items"] == []
