"""Real-provider integrations. Importing this module never contacts a provider."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime
from typing import Any

import httpx


class Mem0Backend:
    """Extract facts with Mem0 in an isolated, disposable workspace.

    Mem0's normal add() mutates its vector store before returning facts. Giving
    each request a local in-memory Qdrant prevents uncommitted model decisions
    from mutating our event-derived production index. Existing-memory merges
    are intentionally explicit operations in the event core, not inferred here.
    """

    def extract(self, content: str, user_id: str, scope: str) -> list[dict]:
        from .config import settings
        from .runtime import resolve_settings

        configured = resolve_settings(settings(), user_id)
        values = {
            "LLM_API_KEY": configured.llm_api_key,
            "LLM_MODEL": configured.llm_model,
            "EMBEDDING_API_KEY": configured.embedding_api_key,
            "EMBEDDING_MODEL": configured.embedding_model,
        }
        required = ("LLM_API_KEY", "LLM_MODEL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL")
        missing = [name for name in required if not values[name]]
        if missing:
            raise RuntimeError("Model configuration missing: " + ", ".join(missing))
        # Disable SDK telemetry before its first import; no hidden cloud client.
        os.environ["MEM0_TELEMETRY"] = "false"
        from mem0 import Memory
        from qdrant_client import QdrantClient

        client = QdrantClient(":memory:")
        config = {
            "version": "v1.1",
            "history_db_path": ":memory:",
            "llm": {
                "provider": "openai",
                "config": {
                    "model": configured.llm_model,
                    "api_key": configured.llm_api_key,
                    "openai_base_url": configured.llm_base_url,
                },
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": configured.embedding_model,
                    "api_key": configured.embedding_api_key,
                    "openai_base_url": configured.embedding_base_url,
                    # Many compatible providers reject the optional dimensions field.
                    "embedding_dims": configured.embedding_dimensions
                    if configured.embedding_send_dimensions
                    else None,
                },
            },
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "client": client,
                    "collection_name": "extraction",
                    "embedding_model_dims": configured.embedding_dimensions,
                },
            },
        }
        memory = None
        try:
            memory = Memory.from_config(config)
            # Mem0 2.2.1 turns malformed extraction JSON and some embedding
            # failures into an empty list. Guard provider boundaries so a job
            # can distinguish "no facts" from a failed extraction pipeline.
            from mem0.memory.utils import extract_json, remove_code_blocks

            original_generate = memory.llm.generate_response

            def checked_generate(*args, **kwargs):
                response = original_generate(*args, **kwargs)
                try:
                    if not isinstance(response, str) or not response.strip():
                        raise ValueError("empty response")
                    cleaned = remove_code_blocks(response)
                    try:
                        decoded = json.loads(cleaned, strict=False)
                    except json.JSONDecodeError:
                        decoded = json.loads(extract_json(cleaned), strict=False)
                    if not isinstance(decoded, dict) or not isinstance(decoded.get("memory"), list):
                        raise ValueError("missing memory list")
                    if any(
                        not isinstance(fact, dict)
                        or not isinstance(fact.get("text"), str)
                        or not fact["text"].strip()
                        for fact in decoded["memory"]
                    ):
                        raise ValueError("invalid fact")
                except (ValueError, TypeError, AttributeError):
                    # Never echo the provider response into SDK logs.
                    raise RuntimeError("Mem0 extraction returned invalid fact JSON") from None
                return json.dumps(decoded, ensure_ascii=False)

            memory.llm.generate_response = checked_generate
            embedding_errors = []

            def checked_embedding(method_name):
                original = getattr(memory.embedding_model, method_name)

                def wrapped(*args, **kwargs):
                    try:
                        return original(*args, **kwargs)
                    except Exception:
                        embedding_errors.append(method_name)
                        raise RuntimeError("Mem0 embedding provider failed") from None

                return wrapped

            memory.embedding_model.embed = checked_embedding("embed")
            memory.embedding_model.embed_batch = checked_embedding("embed_batch")
            # Bound actual provider timeouts below the worker job lease.
            for component in (memory.llm, memory.embedding_model):
                provider_client = getattr(component, "client", None)
                if provider_client is not None:
                    provider_client.timeout = 45
                    provider_client.max_retries = 1
            result = memory.add(content, user_id=user_id, metadata={"scope": scope})
            if embedding_errors:
                raise RuntimeError("Mem0 extraction encountered an embedding failure")
            if not isinstance(result, dict) or not isinstance(result.get("results"), list):
                raise RuntimeError("Mem0 returned an unexpected extraction response")
            facts = []
            for item in result["results"]:
                if item.get("event") == "ADD" and isinstance(item.get("memory"), str):
                    text = item["memory"].strip()
                    if text:
                        facts.append({"content": text, "operation": "ADD"})
            return facts
        finally:
            if memory is not None:
                for component in (memory.llm, memory.embedding_model):
                    provider_client = getattr(component, "client", None)
                    if provider_client is not None:
                        provider_client.close()
                connection = getattr(memory.db, "connection", getattr(memory.db, "conn", None))
                if connection is not None:
                    connection.close()
            client.close()


def _duration(start: Any, end: Any) -> float:
    if not start or not end:
        return 0.0
    try:
        first = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        last = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        return max(0, (last - first).total_seconds() * 1000)
    except (ValueError, TypeError):
        return 0.0


class LangfuseAdapter:
    """Poll the public trace API and extract semantic summaries only.

    Full pagination on every poll deliberately includes late updates to old
    traces. This favors correctness over scale for the single-user test stack.
    Persisted event UUIDs do the deduplication; no volatile 'seen trace' cache.
    The caller must commit all returned events before recording a successful poll.
    """

    def __init__(self, base_url=None, public_key=None, secret_key=None, client=None):
        from .config import settings

        configured = settings()
        self.base_url = (
            base_url
            or configured.langfuse_base_url
            or os.getenv("LANGFUSE_HOST", "http://langfuse-web:3000")
        ).rstrip("/")
        self.public_key = public_key or configured.langfuse_public_key
        self.secret_key = secret_key or configured.langfuse_secret_key
        self.user_id = configured.langfuse_user_id
        self.default_scope = configured.langfuse_scope
        self.client = client

    def _event(self, trace_id, kind, entity_id, payload, stable=False):
        snapshot = (
            ""
            if stable
            else hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
        )
        key = f"{self.base_url}:{trace_id}:{kind}:{entity_id}:{snapshot}"
        return {
            "event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, key)),
            "event_type": kind,
            "entity_type": "skill" if kind == "skill.invoked" else "trace",
            "entity_id": entity_id,
            "payload": payload,
            "metadata": {"source": "langfuse", "source_trace_id": trace_id},
        }

    def normalize(self, trace: dict) -> tuple[str, str, list[dict]]:
        from .config import settings

        configured = settings()
        tid = str(trace["id"])
        meta = trace.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        # Tenant binding belongs to the administrator, never trace input.
        user_id = self.user_id
        scope = str(meta.get("memory_os_scope", self.default_scope))
        common = {"trace_id": tid, "source_trace_id": tid}
        if configured.langfuse_project_id:
            from urllib.parse import quote

            common["langfuse_url"] = (
                configured.langfuse_ui_url.rstrip("/")
                + "/project/"
                + quote(configured.langfuse_project_id, safe="")
                + "/traces/"
                + quote(tid, safe="")
            )
        events = [
            self._event(
                tid,
                "trace.started",
                tid,
                {
                    **common,
                    "task": trace.get("name") or "Langfuse trace",
                    "started_at": trace.get("timestamp"),
                },
            )
        ]
        observations = trace.get("observations") or []
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            oid = str(observation["id"])
            ometa = observation.get("metadata") or {}
            if not isinstance(ometa, dict):
                ometa = {}
            level = str(observation.get("level", "DEFAULT")).upper()
            status = (
                "error"
                if level in ("ERROR", "FATAL")
                else ("success" if observation.get("endTime") else "running")
            )
            skill = ometa.get("skill_name") or ometa.get("skill")
            usage = observation.get("usageDetails") or observation.get("usage") or {}
            total = usage.get("total", usage.get("totalTokens", 0)) or 0
            if not total:
                total = (usage.get("input", usage.get("promptTokens", 0)) or 0) + (
                    usage.get("output", usage.get("completionTokens", 0)) or 0
                )
            if skill:
                events.append(
                    self._event(
                        tid,
                        "skill.invoked",
                        oid,
                        {
                            **common,
                            "span_id": oid,
                            "invocation_id": oid,
                            "skill_name": str(skill),
                            "status": status,
                            "duration_ms": _duration(
                                observation.get("startTime"), observation.get("endTime")
                            ),
                            "tokens": total,
                            "error_type": ometa.get("error_type", "execution_error")
                            if status == "error"
                            else None,
                        },
                    )
                )
            if status == "error":
                events.append(
                    self._event(
                        tid,
                        "trace.error",
                        tid,
                        {
                            **common,
                            "span_id": oid,
                            "error_type": ometa.get("error_type", "execution_error"),
                            "message": str(observation.get("statusMessage") or "Execution failed"),
                        },
                    )
                )
            if ometa.get("retry_attempt") is not None:
                events.append(
                    self._event(
                        tid,
                        "trace.retry",
                        tid,
                        {
                            **common,
                            "span_id": oid,
                            "attempt": ometa["retry_attempt"],
                            "delay_ms": ometa.get("retry_delay_ms", 0),
                        },
                    )
                )
        # A span end is insufficient to infer the whole trace has finished.
        ended = meta.get("trace_finished") or trace.get("endTime")
        if ended:
            events.append(
                self._event(
                    tid,
                    "trace.finished",
                    tid,
                    {
                        **common,
                        "status": meta.get("status", "success"),
                        "total_duration_ms": (trace.get("latency") or 0) * 1000,
                        "total_tokens": (trace.get("usage") or {}).get(
                            "total", meta.get("total_tokens", 0)
                        ),
                    },
                )
            )
        return user_id, scope, events

    def poll(self) -> list[tuple[str, str, list[dict]]]:
        if not self.public_key or not self.secret_key:
            raise RuntimeError("LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required")
        own_client = self.client is None
        client = self.client or httpx.Client(timeout=30)
        results = []
        try:
            page = 1
            while True:
                response = client.get(
                    self.base_url + "/api/public/traces",
                    params={"page": page, "limit": 100},
                    auth=(self.public_key, self.secret_key),
                )
                response.raise_for_status()
                body = response.json()
                traces = body["data"]
                for trace in traces:
                    detail = client.get(
                        self.base_url + "/api/public/traces/" + str(trace["id"]),
                        auth=(self.public_key, self.secret_key),
                    )
                    detail.raise_for_status()
                    results.append(self.normalize(detail.json()))
                if not traces or page >= int((body.get("meta") or {}).get("totalPages", page)):
                    break
                page += 1
            return results
        finally:
            if own_client:
                client.close()
