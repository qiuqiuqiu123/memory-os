import json
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql://memory:memory@localhost:5432/memory"
    api_key: str = ""
    api_keys_json: str = "{}"  # {"opaque-key": "user_id"}, optional future users
    settings_encryption_key: str = ""
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "memory_os"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = ""
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dimensions: int = 1536
    embedding_send_dimensions: bool = False
    langfuse_base_url: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_project_id: str = ""
    langfuse_ui_url: str = "http://localhost:3000"
    langfuse_user_id: str = "default"
    langfuse_scope: str = "default"
    worker_interval: float = 2
    mcp_allowed_hosts: str = "localhost:*,127.0.0.1:*,api:*"

    def keys(self):
        keys = json.loads(self.api_keys_json)
        if self.api_key:
            keys[self.api_key] = "default"
        return keys


@lru_cache
def settings():
    return Settings()
