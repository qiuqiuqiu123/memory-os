"""Runtime settings handoff using the real Mem0 SDK and offline provider fixtures.

No live model traffic or key is required. Persistence/encryption is covered by
the runtime module's own PostgreSQL tests; these tests cover its consumer.
"""

import logging

import pytest

from memory_os.config import Settings
from memory_os.integrations import Mem0Backend


@pytest.fixture
def capture_mem0_configuration(monkeypatch):
    monkeypatch.setenv("MEM0_TELEMETRY", "false")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from mem0 import Memory

    snapshots = []

    def offline_add(self, content, *, user_id, metadata):
        snapshots.append(
            {
                "user_id": user_id,
                "scope": metadata["scope"],
                "llm_model": self.llm.config.model,
                "llm_api_key": self.llm.client.api_key,
                "llm_base_url": str(self.llm.client.base_url),
                "embedding_model": self.embedding_model.config.model,
                "embedding_api_key": self.embedding_model.client.api_key,
                "embedding_base_url": str(self.embedding_model.client.base_url),
                "embedding_dimensions": self.config.vector_store.config.embedding_model_dims,
            }
        )
        return {"results": [{"event": "ADD", "memory": "Offline extracted fact"}]}

    monkeypatch.setattr(Memory, "add", offline_add)
    return snapshots


def test_mem0_reads_current_runtime_configuration_without_restart(
    monkeypatch, caplog, capture_mem0_configuration
):
    import memory_os.config as config
    import memory_os.runtime as runtime

    base = Settings(
        _env_file=None,
        llm_model="base-model",
        llm_api_key="base-llm-key",
        embedding_model="base-embedding",
        embedding_api_key="base-embedding-key",
    )
    monkeypatch.setattr(config, "settings", lambda: base)
    current = [
        base.model_copy(
            update={
                "llm_model": "gpt-4.1-mini",
                "llm_api_key": "runtime-llm-secret-one",
                "llm_base_url": "http://runtime-llm-one/v1",
                "embedding_model": "runtime-embedding-one",
                "embedding_api_key": "runtime-embedding-secret-one",
                "embedding_base_url": "http://runtime-embedding-one/v1",
                "embedding_dimensions": 3,
            }
        )
    ]
    resolutions = []

    def resolve(base_settings, user_id):
        assert base_settings is base
        resolutions.append(user_id)
        return current[0]

    monkeypatch.setattr(runtime, "resolve_settings", resolve)
    backend = Mem0Backend()
    with caplog.at_level(logging.INFO):
        first = backend.extract("first input", "tenant-a", "project")
        current[0] = current[0].model_copy(
            update={
                "llm_model": "gpt-4.1-mini-updated",
                "llm_api_key": "runtime-llm-secret-two",
                "llm_base_url": "http://runtime-llm-two/v1",
                "embedding_model": "runtime-embedding-two",
                "embedding_api_key": "runtime-embedding-secret-two",
                "embedding_base_url": "http://runtime-embedding-two/v1",
                "embedding_dimensions": 4,
            }
        )
        second = backend.extract("second input", "tenant-a", "project")
    assert resolutions == ["tenant-a", "tenant-a"]
    one, two = capture_mem0_configuration
    assert one["llm_model"] == "gpt-4.1-mini"
    assert two["llm_model"] == "gpt-4.1-mini-updated"
    for snapshot, suffix, dimensions in [(one, "one", 3), (two, "two", 4)]:
        assert snapshot["llm_api_key"] == "runtime-llm-secret-" + suffix
        assert snapshot["llm_base_url"] == f"http://runtime-llm-{suffix}/v1/"
        assert snapshot["embedding_model"] == "runtime-embedding-" + suffix
        assert snapshot["embedding_api_key"] == "runtime-embedding-secret-" + suffix
        assert snapshot["embedding_base_url"] == f"http://runtime-embedding-{suffix}/v1/"
        assert snapshot["embedding_dimensions"] == dimensions
    assert first == second == [{"content": "Offline extracted fact", "operation": "ADD"}]
    for secret in (
        "runtime-llm-secret-one",
        "runtime-llm-secret-two",
        "runtime-embedding-secret-one",
        "runtime-embedding-secret-two",
    ):
        assert secret not in caplog.text
        assert secret not in repr(first)


def test_mem0_passes_actual_user_to_deployment_settings_resolver(
    monkeypatch, capture_mem0_configuration
):
    import memory_os.config as config
    import memory_os.runtime as runtime

    base = Settings(_env_file=None)
    monkeypatch.setattr(config, "settings", lambda: base)

    identities = []

    def resolve(base_settings, user_id):
        identities.append(user_id)
        return base_settings.model_copy(
            update={
                "llm_model": "deployment-model",
                "llm_api_key": "deployment-secret",
                "embedding_model": "deployment-embedding",
                "embedding_api_key": "deployment-embed-secret",
                "embedding_dimensions": 3,
            }
        )

    monkeypatch.setattr(runtime, "resolve_settings", resolve)
    backend = Mem0Backend()
    backend.extract("tenant a input", "tenant-a", "shared-scope")
    backend.extract("tenant b input", "tenant-b", "shared-scope")
    assert identities == ["tenant-a", "tenant-b"]
    assert [item["user_id"] for item in capture_mem0_configuration] == identities
    assert all(item["llm_model"] == "deployment-model" for item in capture_mem0_configuration)
