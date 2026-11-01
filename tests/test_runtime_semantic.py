"""Real PostgreSQL/Qdrant hot updates with explicit offline vector fixtures.

No paid/model endpoint is contacted. Each test creates an isolated PostgreSQL
database and Qdrant collections, retained because deletion needs approval.
"""

import os
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_core import core as pg_core  # noqa: F401

from memory_os.config import Settings
from memory_os.core import Core
from memory_os.runtime import public_settings, resolve_settings, save_settings
from memory_os.semantic import Semantic
from memory_os.service import new_event


@pytest.fixture
def runtime_base(pg_core):  # noqa: F811
    qdrant_url = os.getenv("QDRANT_TEST_URL")
    if not qdrant_url:
        pytest.skip("QDRANT_TEST_URL is required")
    name = "dynamic_" + uuid.uuid4().hex
    with psycopg.connect(pg_core.url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params = conninfo_to_dict(pg_core.url)
    params["dbname"] = name
    url = make_conninfo(**params)
    isolated = Core(url)
    isolated.initialize()
    return Settings(
        _env_file=None,
        database_url=url,
        api_key="offline-dynamic-admin",
        settings_encryption_key="offline-dynamic-encryption",
        qdrant_url=qdrant_url,
        qdrant_collection="dynamic_" + uuid.uuid4().hex,
        embedding_model="offline-old-embedding",
        embedding_dimensions=3,
    )


def test_existing_semantic_instance_uses_new_pg_settings_and_collection(runtime_base, monkeypatch):
    base = runtime_base
    store = Core(base.database_url)
    user = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    scope = "dynamic-test"
    store.append(
        user, scope, [new_event("memory.created", mid, {"content": "fixture memory", "tags": []})]
    )
    memory = store.memory(user, scope, mid)
    calls = []
    new_model = "offline-new-embedding-" + uuid.uuid4().hex

    def offline_embed(self, text):
        calls.append(
            (self.s.embedding_model, self.s.embedding_dimensions, self.s.qdrant_collection)
        )
        return [1.0] + [0.0] * (self.s.embedding_dimensions - 1)

    monkeypatch.setattr(Semantic, "embed", offline_embed)
    semantic = Semantic(base)
    try:
        semantic.index(memory)
        assert semantic.search(user, scope, "fixture", 5)[0].payload["memory_id"] == mid
        assert all(item[0] == "offline-old-embedding" for item in calls)
        public = public_settings(base)
        body = {
            "revision": public["revision"],
            "llm": {
                "base_url": "http://offline-llm/v1",
                "model": "gpt-4.1-mini",
                "api_key": "offline-key",
            },
            "embedding": {
                "base_url": "http://offline-embedding/v1",
                "model": new_model,
                "api_key": "offline-key",
                "dimensions": 4,
                "send_dimensions": False,
            },
            "reindex": True,
        }
        switched = save_settings(base, body)
        current = resolve_settings(base, user)
        assert switched["collection"] == current.qdrant_collection != base.qdrant_collection
        # The same long-lived Semantic instance immediately reads the new space.
        calls.clear()
        assert semantic.search(user, scope, "fixture", 5) == []
        assert calls[0] == (new_model, 4, switched["collection"])
        with store.connection() as conn:
            jobs = conn.execute(
                "SELECT * FROM jobs WHERE kind='vector' AND dedupe LIKE 'settings:%%'"
            ).fetchall()
        assert len(jobs) == 1 and jobs[0]["payload"]["memory_id"] == mid
        semantic.index(memory)
        hits = semantic.search(user, scope, "fixture", 5)
        assert hits[0].payload["memory_id"] == mid
        # Old vectors remain physically separate and never contaminate new search.
        assert semantic.q.count(base.qdrant_collection, exact=True).count == 1
        assert semantic.q.count(switched["collection"], exact=True).count == 1
        assert all(call[0] == new_model for call in calls)
    finally:
        semantic.q.close()


def test_settings_validation_error_never_echoes_provider_key(runtime_base, monkeypatch):
    import importlib

    from fastapi.testclient import TestClient

    from memory_os.config import settings

    base = runtime_base
    monkeypatch.setenv("DATABASE_URL", base.database_url)
    monkeypatch.setenv("API_KEY", base.api_key)
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", base.settings_encryption_key)
    settings.cache_clear()
    import memory_os.app as application

    app = importlib.reload(application)
    secret = "do-not-echo-this-provider-secret"
    body = {
        "revision": 0,
        "llm": {"base_url": "http://llm/v1", "model": "fixture", "api_key": secret},
        "embedding": {"base_url": "http://embed/v1", "model": "fixture", "api_key": secret},
    }
    try:
        with TestClient(app.app, base_url="http://localhost:8000") as client:
            headers = {"Authorization": "Bearer " + base.api_key}
            # Missing dimensions previously included the entire nested object
            # in Pydantic's input field, exposing its api_key in the 422 body.
            response = client.put("/api/v1/settings", headers=headers, json=body)
            assert response.status_code == 422
            assert secret not in response.text
            assert all(
                "input" not in error and "ctx" not in error for error in response.json()["detail"]
            )
            body["embedding"]["dimensions"] = 3
            body["llm"]["api_key"] = secret * 300
            response = client.put("/api/v1/settings", headers=headers, json=body)
            assert response.status_code == 422
            assert secret not in response.text
    finally:
        settings.cache_clear()
