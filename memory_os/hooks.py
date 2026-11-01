"""Claude Code command hook: python -m memory_os.hooks <hook-name>.

Only injection JSON goes to stdout. Provider outages leave capture events in a
durable local outbox and never block the user's agent run.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx

from .client import MemoryClient


def _now():
    return datetime.now(timezone.utc).isoformat()


def hook_events(name, data):
    session = str(data.get("session_id", "unknown"))
    tid = str(uuid.uuid5(uuid.NAMESPACE_URL, "claude-code-session:" + session))
    tool_id = str(data.get("tool_use_id", ""))
    sid = str(uuid.uuid5(uuid.NAMESPACE_URL, tid + ":" + tool_id))
    tool = str(data.get("tool_name", "tool"))
    metadata = {"source": "claude-code", "source_trace_id": tid}

    def event(kind, payload, entity=None):
        # UUID lives in the durable outbox, so network retries reuse it exactly.
        # Repeated hook invocations may have different timestamps and must not
        # collide with an earlier payload under a deterministic event UUID.
        return {
            "event_id": str(uuid.uuid4()),
            "event_type": kind,
            "entity_type": "skill" if kind == "skill.invoked" else "trace",
            "entity_id": entity or tid,
            "payload": {"trace_id": tid, **payload},
            "metadata": metadata,
        }

    if name == "SessionStart":
        return [
            event(
                "trace.started",
                {
                    "task": "Claude Code session",
                    "agent_version": "claude-code",
                    "started_at": _now(),
                },
            )
        ]
    if name == "PreToolUse":
        return [
            event(
                "trace.span.opened",
                {
                    "span_id": sid,
                    "span_type": "tool",
                    "name": tool,
                    "parent_span_id": None,
                    "started_at": _now(),
                    "input_summary": "[capture omitted]",
                },
                sid,
            )
        ]
    if name in ("PostToolUse", "PostToolUseFailure"):
        failed = name == "PostToolUseFailure"
        status = "error" if failed else "success"
        output = [
            event(
                "trace.span.closed",
                {
                    "span_id": sid,
                    "status": status,
                    "ended_at": _now(),
                    "output_summary": "[capture omitted]",
                    "tokens": 0,
                },
                sid,
            )
        ]
        if tool == "Skill":
            skill = (data.get("tool_input") or {}).get("skill", "unknown")
            output.append(
                event(
                    "skill.invoked",
                    {
                        "span_id": sid,
                        "invocation_id": sid,
                        "skill_name": skill,
                        "status": status,
                        "tokens": 0,
                    },
                    sid,
                )
            )
        if failed:
            output.append(
                event(
                    "trace.error",
                    {
                        "span_id": sid,
                        "error_type": "tool_failure",
                        "message": "Tool execution failed",
                    },
                )
            )
        return output
    if name == "Stop":
        # Each completed turn can update an existing session-level trace.
        result = event("trace.finished", {"status": "success", "ended_at": _now()})
        result["event_id"] = str(uuid.uuid4())
        return [result]
    return []


def _langfuse_capture(name, data, events, http):
    """Public ingestion API. Returns False when not configured or unavailable."""
    host = os.getenv("LANGFUSE_BASE_URL", os.getenv("LANGFUSE_HOST", "")).rstrip("/")
    public, secret = os.getenv("LANGFUSE_PUBLIC_KEY"), os.getenv("LANGFUSE_SECRET_KEY")
    if not host or not public or not secret:
        return False
    batch = []
    for item in events:
        kind, payload = item["event_type"], item["payload"]
        metadata = {"memory_os_scope": os.getenv("MEMORY_OS_SCOPE", "default")}
        if kind == "trace.started":
            typ, body = (
                "trace-create",
                {
                    "id": payload["trace_id"],
                    "name": payload["task"],
                    "timestamp": payload["started_at"],
                    "metadata": metadata,
                },
            )
        elif kind == "trace.finished":
            typ, body = (
                "trace-create",
                {
                    "id": payload["trace_id"],
                    "metadata": {
                        **metadata,
                        "trace_finished": True,
                        "status": payload["status"],
                    },
                },
            )
        elif kind == "trace.span.opened":
            skill = (data.get("tool_input") or {}).get("skill")
            if skill:
                metadata["skill_name"] = skill
            typ, body = (
                "span-create",
                {
                    "id": payload["span_id"],
                    "traceId": payload["trace_id"],
                    "name": payload["name"],
                    "startTime": payload["started_at"],
                    "metadata": metadata,
                },
            )
        elif kind == "trace.span.closed":
            failed = payload["status"] == "error"
            typ, body = (
                "span-update",
                {
                    "id": payload["span_id"],
                    "traceId": payload["trace_id"],
                    "endTime": payload["ended_at"],
                    "level": "ERROR" if failed else "DEFAULT",
                    "statusMessage": "Tool execution failed" if failed else None,
                },
            )
        else:
            # Skill/error semantic summaries are derived by the server adapter.
            continue
        batch.append({"id": item["event_id"], "timestamp": _now(), "type": typ, "body": body})
    if not batch:
        return True
    try:
        response = http.post(
            host + "/api/public/ingestion",
            auth=(public, secret),
            json={"batch": batch},
            timeout=3,
        )
        response.raise_for_status()
        body = response.json()
        return not body.get("errors")
    except (httpx.HTTPError, ValueError):
        return False


def run_hook(name, data, client, trace_http=None):
    events = hook_events(name, data)
    # Persist timings across separate hook processes, including the offline path.
    client.db.execute(
        "CREATE TABLE IF NOT EXISTS hook_timings (span_id TEXT PRIMARY KEY, started REAL NOT NULL)"
    )
    if name == "PreToolUse" and events:
        with client.db:
            client.db.execute(
                "INSERT OR REPLACE INTO hook_timings VALUES (?,?)",
                (events[0]["payload"]["span_id"], time.time()),
            )
    elif name in ("PostToolUse", "PostToolUseFailure") and events:
        sid = events[0]["payload"]["span_id"]
        row = client.db.execute(
            "SELECT started FROM hook_timings WHERE span_id=?", (sid,)
        ).fetchone()
        if row:
            duration = max(0, (time.time() - row[0]) * 1000)
            for event in events:
                if event["event_type"] in ("trace.span.closed", "skill.invoked"):
                    event["payload"]["duration_ms"] = duration
            with client.db:
                client.db.execute("DELETE FROM hook_timings WHERE span_id=?", (sid,))
    if events:
        captured = False
        if trace_http is not None:
            captured = _langfuse_capture(name, data, events, trace_http)
        if not captured:
            for event in events:
                client.enqueue(event)
    try:
        client.push()
    except httpx.HTTPError:
        pass
    trigger = None
    context = {
        "trace_id": str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "claude-code-session:" + str(data.get("session_id", "unknown")),
            )
        )
    }
    if name == "SessionStart":
        trigger = "session.start"
    elif name == "UserPromptSubmit":
        trigger = "task.context"
        # Only explicit retrieval context, not persisted as raw trace input.
        context["query"] = str(data.get("prompt", ""))[:4000]
    elif name == "PreToolUse" and data.get("tool_name") == "Skill":
        trigger = "skill.invoked"
        context["skill_name"] = (data.get("tool_input") or {}).get("skill", "")
    if trigger:
        try:
            result = client.inject(trigger, context, channel="claude-code-hook")
            content = result.get("content", "")
            if content:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": name,
                        "additionalContext": content,
                    }
                }
        except httpx.HTTPError:
            pass
    return {}


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    client = None
    try:
        data = json.load(sys.stdin)
        with httpx.Client(timeout=4) as http:
            base = os.getenv("MEMORY_OS_URL", "http://localhost:8000")
            key = os.getenv("MEMORY_OS_API_KEY", "")
            scope = os.getenv("MEMORY_OS_SCOPE", "default")
            suffix = hashlib.sha256(
                json.dumps([base.rstrip("/"), scope, key]).encode()
            ).hexdigest()[:20]
            client = MemoryClient(
                base,
                key,
                scope,
                os.path.expanduser(
                    os.getenv("MEMORY_OS_CACHE", f"~/.memory-os/client-{suffix}.sqlite3")
                ),
                client=http,
            )
            result = run_hook(name, data, client, http)
            if result:
                print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        # Avoid leaking trace input or credentials through detailed exceptions.
        print("memory-os hook deferred: " + type(exc).__name__, file=sys.stderr)
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
