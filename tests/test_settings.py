"""Real PostgreSQL runtime settings; no real provider calls in this module."""

import uuid

import pytest
from fastapi.testclient import TestClient
from test_core import core, event  # noqa: F401 - imported pytest fixture

from memory_os.config import Settings, settings
from memory_os.core import Conflict
from memory_os.runtime import public_settings, resolve_settings, save_settings


@pytest.fixture
def base(core):  # noqa: F811 - pytest injects the imported fixture
    # Isolated DB for deployment-wide settings, created once for this suite.
    name = "settings_" + uuid.uuid4().hex
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(core.url)
    with psycopg.connect(core.url, autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(name)))
    params["dbname"] = name
    from memory_os.core import Core

    c = Core(make_conninfo(**params))
    c.initialize()
    return Settings(
        database_url=c.url,
        api_key="settings-test-admin",
        settings_encryption_key="test-master",
        embedding_base_url="https://old.example.test/v1",
        embedding_model="old-model",
        embedding_dimensions=1536,
        _env_file=None,
    )


def body(base):
    d = public_settings(base)
    for section in ("llm", "embedding"):
        d[section].pop("api_key_set")
    d.pop("admin")
    d.pop("collection")
    d["llm"].update(
        base_url="https://api.example.test/v1", model="test-model", api_key="secret-provider-token"
    )
    d["embedding"].update(
        base_url="http://bge-m3:8080/v1", model="bge-m3", dimensions=1024, api_key="bge-secret"
    )
    return d


def test_encrypt_mask_keep_and_hot_reload(base):
    b = body(base)
    res = save_settings(base, b)
    assert res["revision"] == 1 and res["llm"]["api_key_set"]
    assert "secret-provider-token" not in str(res) and "api_key" not in res["llm"]
    with __import__("psycopg").connect(base.database_url) as conn:
        raw = conn.execute("SELECT encrypted FROM model_settings").fetchone()[0]
        assert "secret-provider-token" not in raw and "bge-secret" not in raw
    assert resolve_settings(base).llm_api_key == "secret-provider-token"
    b["revision"] = 1
    b["llm"].update(api_key="", model="changed-model")
    save_settings(base, b)
    assert resolve_settings(base).llm_model == "changed-model"
    assert resolve_settings(base).llm_api_key == "secret-provider-token"
    with pytest.raises(Conflict):
        save_settings(base, b)


def test_vector_space_switch_requires_reindex_and_enqueues(base):
    from memory_os.core import Core

    c = Core(base.database_url)
    uid = str(uuid.uuid4())
    c.append(uid, "project", [event(content="remember me")])
    b = body(base)
    with pytest.raises(Conflict):
        save_settings(base, b)
    b["reindex"] = True
    res = save_settings(base, b)
    assert res["collection"] != base.qdrant_collection
    with c.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM jobs WHERE dedupe LIKE 'settings:%%'"
            ).fetchone()["n"]
            == 1
        )
    assert len(c.memories(uid, "project")) == 1


def test_admin_only_and_secret_not_in_errors(base, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", base.database_url)
    monkeypatch.setenv("API_KEY", base.api_key)
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", base.settings_encryption_key)
    monkeypatch.setenv("API_KEYS_JSON", '{"ordinary":"other"}')
    settings.cache_clear()
    import importlib

    import memory_os.app

    app = importlib.reload(memory_os.app)
    with TestClient(app.app, base_url="http://localhost:8000") as client:
        assert client.get("/api/v1/settings").status_code == 401
        assert (
            client.get("/api/v1/settings", headers={"Authorization": "Bearer ordinary"}).status_code
            == 403
        )
        headers = {"Authorization": "Bearer " + base.api_key}
        result = client.put("/api/v1/settings", headers=headers, json=body(base))
        assert result.status_code == 200, result.text
        assert "secret-provider-token" not in result.text
        assert (
            client.get("/api/v1/settings", headers=headers).json()["llm"]["model"] == "test-model"
        )
    settings.cache_clear()
