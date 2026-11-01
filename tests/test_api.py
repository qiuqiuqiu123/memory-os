import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL required")
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("API_KEYS_JSON", '{"other-key":"other"}')
    from memory_os.config import settings

    settings.cache_clear()
    import importlib

    import memory_os.app

    module = importlib.reload(memory_os.app)
    with TestClient(module.app, base_url="http://localhost:8000") as c:
        yield c


def test_auth_and_tenant_binding(client):
    assert client.get("/api/v1/status").status_code == 401
    assert client.post("/mcp/", json={}).status_code == 401
    scope = str(uuid.uuid4())
    headers = {"Authorization": "Bearer test-key"}
    result = client.post(
        "/api/v1/memories",
        headers=headers,
        json={"content": "private", "scope": scope, "infer": False, "user_id": "other"},
    )
    assert result.status_code == 200, result.text
    assert (
        len(
            client.get("/api/v1/memories", headers=headers, params={"scope": scope}).json()["items"]
        )
        == 1
    )
    assert (
        client.get(
            "/api/v1/memories",
            headers={"Authorization": "Bearer other-key"},
            params={"scope": scope},
        ).json()["items"]
        == []
    )
    assert client.post("/api/v1/inject", headers=headers, json={"budget": 501}).status_code == 422


def test_no_silent_semantic_fallback(client, monkeypatch):
    import memory_os.app

    def unavailable(*args, **kwargs):
        raise RuntimeError("explicit embedding outage fixture")

    monkeypatch.setattr(memory_os.app.service.semantic, "search", unavailable)
    result = client.post(
        "/api/v1/recall",
        headers={"Authorization": "Bearer test-key"},
        json={"query": "language preference"},
    )
    assert result.status_code == 503


def test_trace_tree_and_semantic_mcp_tool_list(client):
    headers = {"Authorization": "Bearer test-key"}
    scope = str(uuid.uuid4())
    tid = str(uuid.uuid4())
    span = str(uuid.uuid4())

    def ev(kind, entity, payload):
        return {
            "event_id": str(uuid.uuid4()),
            "entity_type": "trace",
            "entity_id": entity,
            "event_type": kind,
            "payload": payload,
        }

    events = [
        ev("trace.started", tid, {"task": "integration"}),
        ev(
            "trace.span.opened",
            span,
            {"trace_id": tid, "span_type": "tool", "name": "shell"},
        ),
        ev(
            "trace.error",
            tid,
            {"span_id": span, "message": "failed", "error_type": "timeout"},
        ),
    ]
    assert (
        client.post(
            "/api/v1/events", headers=headers, json={"scope": scope, "events": events}
        ).status_code
        == 200
    )
    result = client.get("/api/v1/traces/" + tid, headers=headers, params={"scope": scope}).json()
    assert result["spans"][0]["errors"][0]["error_type"] == "timeout"
    mcp = client.post(
        "/mcp/",
        headers={**headers, "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert mcp.status_code == 200, mcp.text
    assert "remember" in mcp.text and "events_sync" in mcp.text
    called = client.post(
        "/mcp/",
        headers={**headers, "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "remember",
                "arguments": {
                    "content": "MCP real protocol test",
                    "scope": scope,
                    "infer": False,
                },
            },
        },
    )
    assert called.status_code == 200, called.text
    assert "memory_id" in called.text and 'isError":true' not in called.text


def _mcp_result(response):
    assert response.status_code == 200, response.text
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()["result"]
    messages = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    return next(message["result"] for message in messages if "result" in message)


def _capture_call(client, arguments, key="test-key"):
    return client.post(
        "/mcp/",
        headers={"Authorization": "Bearer " + key, "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 91,
            "method": "tools/call",
            "params": {"name": "capture_report", "arguments": arguments},
        },
    )


def _structured_tool(result):
    assert not result.get("isError"), result
    if "structuredContent" in result:
        return result["structuredContent"]
    return json.loads(next(block["text"] for block in result["content"] if block["type"] == "text"))


def test_mcp_capture_report_auth_idempotency_and_tenant_scope_isolation(client):
    scope = "mcp-capture-" + uuid.uuid4().hex
    args = {
        "scope": scope,
        "session_id": str(uuid.uuid4()),
        "batch_id": str(uuid.uuid4()),
        "events": [
            {
                "source_id": "model-event-1",
                "type": "assistant_message",
                "timestamp": "2026-11-01T01:00:00Z",
                "body": {"text": "Supplemental evidence"},
            }
        ],
        "turn_complete": False,
    }
    assert _capture_call(client, args, key="invalid").status_code == 401
    first = _structured_tool(_mcp_result(_capture_call(client, args)))
    assert first["accepted"] == 1 and first["batch_id"] == args["batch_id"]
    assert first["review_job_ids"] == []
    duplicate = _structured_tool(_mcp_result(_capture_call(client, args)))
    assert duplicate["accepted"] == 0 and duplicate["duplicate"]
    assert duplicate["session_id"] == first["session_id"]
    other = _structured_tool(_mcp_result(_capture_call(client, args, key="other-key")))
    assert other["accepted"] == 1 and other["session_id"] != first["session_id"]
    headers = {"Authorization": "Bearer test-key"}
    assert (
        client.get(
            "/api/v1/capture/sessions/" + other["session_id"],
            headers=headers,
            params={"scope": scope},
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/api/v1/capture/sessions/" + first["session_id"],
            headers=headers,
            params={"scope": "other-scope"},
        ).status_code
        == 404
    )
    changed = {**args, "events": [{**args["events"][0], "body": {"text": "Different content"}}]}
    assert _mcp_result(_capture_call(client, changed))["isError"]
    changed["batch_id"] = str(uuid.uuid4())
    assert _mcp_result(_capture_call(client, changed))["isError"]
    # Explicit reports are retained as evidence, never promoted into memory.
    memories = client.get("/api/v1/memories", headers=headers, params={"scope": scope}).json()
    assert memories["items"] == []


def test_mcp_capture_report_turn_complete_queues_one_review_and_validates_events(client):
    scope = "mcp-capture-" + uuid.uuid4().hex
    args = {
        "scope": scope,
        "session_id": str(uuid.uuid4()),
        "batch_id": str(uuid.uuid4()),
        "events": [
            {
                "source_id": "tool-1",
                "type": "tool_result",
                "timestamp": "2026-11-01T01:00:00Z",
                "body": {"text": "Explicit tool result"},
            }
        ],
        "turn_complete": True,
    }
    first = _structured_tool(_mcp_result(_capture_call(client, args)))
    assert len(first["review_job_ids"]) == 1
    duplicate = _structured_tool(_mcp_result(_capture_call(client, args)))
    assert duplicate["review_job_ids"] == first["review_job_ids"]
    headers = {"Authorization": "Bearer test-key"}
    detail = client.get(
        "/api/v1/capture/sessions/" + first["session_id"], headers=headers, params={"scope": scope}
    ).json()
    assert len(detail["jobs"]) == 1
    assert detail["jobs"][0]["kind"] == "capture_review"
    assert detail["jobs"][0]["status"] == "pending"
    invalid = {
        **args,
        "batch_id": str(uuid.uuid4()),
        "events": [{**args["events"][0], "source_id": "private-reasoning", "type": "reasoning"}],
    }
    assert _mcp_result(_capture_call(client, invalid))["isError"]


def test_mcp_capture_report_schema_and_honest_tool_description_without_database():
    import asyncio

    from memory_os.app import mcp

    tool = next(tool for tool in asyncio.run(mcp.list_tools()) if tool.name == "capture_report")
    assert set(tool.inputSchema["required"]) == {"scope", "session_id", "batch_id", "events"}
    assert tool.inputSchema["properties"]["events"]["type"] == "array"
    assert "not automatic complete" in tool.description
    assert "prevent recursive" in tool.description
