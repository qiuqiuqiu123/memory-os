"""Encrypted server-side model settings, loaded afresh by API and worker."""

import base64
import hashlib
import json
import time
from urllib.parse import urlsplit

import psycopg
from cryptography.fernet import Fernet, InvalidToken
from psycopg.rows import dict_row

from .core import Conflict, InvalidEvent

MODEL_FIELDS = (
    "llm_base_url",
    "llm_model",
    "llm_api_key",
    "embedding_base_url",
    "embedding_model",
    "embedding_api_key",
    "embedding_dimensions",
    "embedding_send_dimensions",
    "qdrant_collection",
)


def cipher(base):
    secret = base.settings_encryption_key or base.api_key
    if not secret:
        raise InvalidEvent("SETTINGS_ENCRYPTION_KEY must be configured")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def _read(base, conn):
    row = conn.execute("SELECT * FROM model_settings WHERE singleton=TRUE").fetchone()
    if not row:
        return 0, {}, base
    try:
        values = json.loads(cipher(base).decrypt(row["encrypted"].encode()))
    except InvalidToken:
        raise RuntimeError(
            "Cannot decrypt model settings; restore the original SETTINGS_ENCRYPTION_KEY"
        ) from None
    return row["revision"], values, base.model_copy(update=values)


def resolve_settings(base, user_id="default"):
    # Models are deployment-wide. Only bootstrap admin may save them; caller
    # identity still determines all memory/trace data isolation.
    if not base.settings_encryption_key and not base.api_key:
        return base
    with psycopg.connect(base.database_url, row_factory=dict_row) as conn:
        return _read(base, conn)[2]


def public_settings(base):
    with psycopg.connect(base.database_url, row_factory=dict_row) as conn:
        revision, _, effective = _read(base, conn)
    return {
        "revision": revision,
        "admin": True,
        "llm": {
            "base_url": effective.llm_base_url,
            "model": effective.llm_model,
            "api_key_set": bool(effective.llm_api_key),
        },
        "embedding": {
            "base_url": effective.embedding_base_url,
            "model": effective.embedding_model,
            "api_key_set": bool(effective.embedding_api_key),
            "dimensions": effective.embedding_dimensions,
            "send_dimensions": effective.embedding_send_dimensions,
        },
        "collection": effective.qdrant_collection,
    }


def validate_url(value):
    url = urlsplit(value)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise InvalidEvent(
            "Model URL must be http(s) without embedded credentials, query or fragment"
        )
    return value.rstrip("/")


def save_settings(base, body):
    from .core import Core

    core = Core(base.database_url)
    with core.connection() as conn:
        # Same ordering as event writes/rebuild. New vector jobs and settings
        # switch are atomic, old in-flight jobs can only affect old collections.
        conn.execute("SELECT version FROM event_clock WHERE singleton=TRUE FOR UPDATE")
        revision, values, effective = _read(base, conn)
        if revision != body["revision"]:
            raise Conflict("Settings changed; reload before saving")
        new = {k: getattr(effective, k) for k in MODEL_FIELDS}
        for section, fields in (
            ("llm", ("base_url", "model", "api_key")),
            ("embedding", ("base_url", "model", "api_key", "dimensions", "send_dimensions")),
        ):
            for name in fields:
                if name not in body[section]:
                    continue
                value = body[section][name]
                if name == "api_key" and (value is None or not value.strip()):
                    continue
                if name == "base_url":
                    value = validate_url(value)
                if name == "model" and not value.strip():
                    raise InvalidEvent("Model name must not be empty")
                new[f"{section}_{name}"] = value
        identity = (
            "embedding_base_url",
            "embedding_model",
            "embedding_dimensions",
            "embedding_send_dimensions",
        )
        changed = any(new[k] != getattr(effective, k) for k in identity)
        count = conn.execute("SELECT count(*) AS n FROM memory_items").fetchone()["n"]
        if changed and count and not body.get("reindex"):
            raise Conflict("Embedding space changed; confirm reindex to rebuild all memories")
        if changed or body.get("reindex"):
            digest = hashlib.sha256(
                json.dumps([new[k] for k in identity], sort_keys=True).encode()
            ).hexdigest()[:12]
            namespace = hashlib.sha256(base.qdrant_collection.encode()).hexdigest()[:8]
            new["qdrant_collection"] = f"memory_os_{namespace}_{digest}_r{revision + 1}"
            for row in conn.execute(
                "SELECT user_id,scope,id,version FROM memory_items WHERE NOT deleted"
            ).fetchall():
                core.enqueue(
                    row["user_id"],
                    row["scope"],
                    "vector",
                    {"memory_id": row["id"], "version": row["version"]},
                    f"settings:{revision + 1}:{row['scope']}:{row['id']}",
                    conn,
                )
        encrypted = cipher(base).encrypt(json.dumps(new).encode()).decode()
        conn.execute(
            "INSERT INTO model_settings(singleton,revision,encrypted) VALUES(TRUE,%s,%s) ON CONFLICT(singleton) DO UPDATE SET revision=EXCLUDED.revision,encrypted=EXCLUDED.encrypted,updated_at=now()",
            (revision + 1, encrypted),
        )
    return public_settings(base)


def test_connection(base, target):
    from openai import OpenAI

    from .semantic import Semantic

    active = resolve_settings(base)
    started = time.monotonic()
    try:
        if target == "embedding":
            semantic = Semantic(active, dynamic=False)
            try:
                vector = semantic.embed("记忆系统 connection test")
            finally:
                semantic.q.close()
            return {
                "ok": True,
                "detail": "向量接口可用，维度与配置一致",
                "dimensions": len(vector),
                "latency_ms": round((time.monotonic() - started) * 1000),
            }
        if not active.llm_api_key or not active.llm_model:
            raise InvalidEvent("请先填写并保存LLM模型和密钥")
        with OpenAI(
            api_key=active.llm_api_key, base_url=active.llm_base_url, timeout=30, max_retries=0
        ) as client:
            result = client.chat.completions.create(
                model=active.llm_model,
                messages=[
                    {
                        "role": "user",
                        "content": "Return a JSON object with key ok and boolean value true.",
                    }
                ],
                response_format={"type": "json_object"},
            )
        obj = json.loads(result.choices[0].message.content)
        if not isinstance(obj, dict) or obj.get("ok") is not True:
            raise ValueError("unexpected JSON response")
        return {
            "ok": True,
            "detail": "LLM接口与JSON输出可用",
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
    except InvalidEvent:
        raise
    except Exception as exc:
        # No provider response text: it can contain secrets or private content.
        return {
            "ok": False,
            "detail": f"{type(exc).__name__}：连接失败，请检查地址、密钥、模型及网络",
        }
