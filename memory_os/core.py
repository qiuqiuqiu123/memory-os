"""Transactional event store. The clock row serializes allocation until commit.

Never advance a pull cursor using the acknowledgement of a push request.
"""

import hashlib
import json
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

TYPES = {
    "memory.created",
    "memory.updated",
    "memory.deleted",
    "memory.retrieved",
    "memory.conflict_resolved",
    "memory.injected",
    "trace.started",
    "trace.finished",
    "trace.span.opened",
    "trace.span.closed",
    "trace.error",
    "trace.retry",
    "skill.invoked",
    "sync.pulled",
}


class Conflict(ValueError):
    pass


class InvalidEvent(ValueError):
    pass


class Core:
    def __init__(self, url):
        self.url = url

    @contextmanager
    def connection(self):
        with psycopg.connect(self.url, row_factory=dict_row) as conn:
            yield conn

    def initialize(self):
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(718252)")
            conn.execute(Path(__file__).with_name("schema.sql").read_text())
        from .capture import init_schema

        init_schema(self)

    def append(self, user, scope, events, conn=None):
        if conn is None:
            with self.connection() as tx:
                return self.append(user, scope, events, tx)
        if not scope or len(scope) > 256:
            raise InvalidEvent("scope must contain 1..256 characters")
        if not 1 <= len(events) <= 500:
            raise InvalidEvent("batch must contain 1..500 events")
        clock = conn.execute(
            "SELECT version FROM event_clock WHERE singleton=TRUE FOR UPDATE"
        ).fetchone()["version"]
        accepted = 0
        versions = []
        for raw in events:
            e = dict(raw)
            try:
                eid = str(uuid.UUID(str(e["event_id"])))
            except (ValueError, KeyError, TypeError) as exc:
                raise InvalidEvent("event_id must be UUID") from exc
            kind = e.get("event_type")
            entity = e.get("entity_id")
            payload = e.get("payload", {})
            if (
                kind not in TYPES
                or not isinstance(entity, str)
                or not entity
                or len(entity) > 256
                or not isinstance(payload, dict)
            ):
                raise InvalidEvent("invalid event type, entity or payload")
            expected = (
                "memory"
                if kind.startswith("memory.")
                else (
                    "skill"
                    if kind == "skill.invoked"
                    else ("system" if kind.startswith("sync.") else "trace")
                )
            )
            if e.get("entity_type") != expected:
                raise InvalidEvent("entity_type does not match event_type")
            metadata = e.get("metadata", {})
            if not isinstance(metadata, dict):
                raise InvalidEvent("metadata must be object")
            canonical = json.dumps(
                [scope, kind, expected, entity, payload, metadata],
                sort_keys=True,
                ensure_ascii=False,
            )
            if len(canonical.encode()) > 262144:
                raise InvalidEvent("event exceeds 256 KiB")
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            old = conn.execute(
                "SELECT id,request_hash FROM events WHERE user_id=%s AND event_id=%s",
                (user, eid),
            ).fetchone()
            if old:
                if old["request_hash"] != digest:
                    raise Conflict("event_id reused with different content")
                versions.append(old["id"])
                continue
            # Canonical content is captured once; replay never calls a model.
            payload = dict(payload)
            if kind in ("memory.updated", "memory.deleted"):
                prior = conn.execute(
                    "SELECT * FROM memory_items WHERE user_id=%s AND scope=%s AND id=%s",
                    (user, scope, entity),
                ).fetchone()
                if not prior or prior["deleted"]:
                    raise InvalidEvent("memory does not exist in this scope")
                if kind == "memory.updated":
                    payload["old_content"] = prior["content"]
            if kind in ("memory.created", "memory.updated"):
                content = payload.get("content" if kind == "memory.created" else "new_content")
                if not isinstance(content, str) or not content.strip() or len(content) > 100000:
                    raise InvalidEvent("memory content must contain 1..100000 characters")
                if (
                    kind == "memory.created"
                    and conn.execute(
                        "SELECT 1 FROM memory_items WHERE user_id=%s AND scope=%s AND id=%s",
                        (user, scope, entity),
                    ).fetchone()
                ):
                    raise Conflict("memory ID already exists; use memory.updated")
                if "tags" in payload and (
                    not isinstance(payload["tags"], list)
                    or any(not isinstance(t, str) for t in payload["tags"])
                ):
                    raise InvalidEvent("tags must be strings")
            if kind.startswith("trace.span.") and not payload.get("trace_id"):
                raise InvalidEvent("trace.span requires trace_id")
            if kind == "skill.invoked":
                if not isinstance(payload.get("skill_name"), str) or not payload["skill_name"]:
                    raise InvalidEvent("skill_name required")
                for number in ("duration_ms", "tokens"):
                    value = payload.get(number, 0)
                    if not isinstance(value, (int, float)) or value < 0:
                        raise InvalidEvent(f"{number} must be nonnegative")
            clock += 1
            row = conn.execute(
                "INSERT INTO events(id,event_id,user_id,scope,event_type,entity_type,entity_id,payload,metadata,request_hash) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    clock,
                    eid,
                    user,
                    scope,
                    kind,
                    expected,
                    entity,
                    Jsonb(payload),
                    Jsonb(metadata),
                    digest,
                ),
            ).fetchone()
            self.project(conn, row)
            accepted += 1
            versions.append(clock)
        conn.execute("UPDATE event_clock SET version=%s WHERE singleton=TRUE", (clock,))
        return {
            "accepted": accepted,
            "duplicates": len(events) - accepted,
            "last_version": max(versions),
            "versions": versions,
        }

    def project(self, conn, e, enqueue=True):
        p = e["payload"]
        kind = e["event_type"]
        key = (e["user_id"], e["scope"], e["entity_id"])
        v = e["id"]
        if kind == "memory.created":
            conn.execute(
                "INSERT INTO memory_items(user_id,scope,id,content,tags,source_trace_id,version,metadata) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    *key,
                    p["content"],
                    Jsonb(p.get("tags", [])),
                    p.get("source_trace_id"),
                    v,
                    Jsonb(p.get("metadata", {})),
                ),
            )
        elif kind == "memory.updated":
            conn.execute(
                "UPDATE memory_items SET content=%s,tags=COALESCE(%s,tags),version=%s WHERE user_id=%s AND scope=%s AND id=%s",
                (p["new_content"], Jsonb(p["tags"]) if "tags" in p else None, v, *key),
            )
        elif kind == "memory.deleted":
            conn.execute(
                "UPDATE memory_items SET deleted=TRUE,version=%s WHERE user_id=%s AND scope=%s AND id=%s",
                (v, *key),
            )
        if kind in ("memory.created", "memory.updated", "memory.deleted"):
            row = conn.execute(
                "SELECT * FROM memory_items WHERE user_id=%s AND scope=%s AND id=%s",
                key,
            ).fetchone()
            if "preference" in row["tags"]:
                conn.execute(
                    "INSERT INTO user_prefs VALUES(%s,%s,%s,%s,%s) ON CONFLICT(user_id,scope,id) DO UPDATE SET data=EXCLUDED.data,version=EXCLUDED.version",
                    (
                        *key,
                        Jsonb({"content": row["content"], "deleted": row["deleted"]}),
                        v,
                    ),
                )
            else:
                conn.execute(
                    "DELETE FROM user_prefs WHERE user_id=%s AND scope=%s AND id=%s",
                    key,
                )
            if enqueue:
                self.enqueue(
                    e["user_id"],
                    e["scope"],
                    "vector",
                    {"memory_id": e["entity_id"], "version": v},
                    f"vector:{v}",
                    conn,
                )
        if kind.startswith("trace."):
            trace_id = (
                p.get("trace_id", e["entity_id"])
                if kind.startswith("trace.span.")
                else e["entity_id"]
            )
            rootkey = (e["user_id"], e["scope"], trace_id)
            old = conn.execute(
                "SELECT data FROM trace_roots WHERE user_id=%s AND scope=%s AND id=%s",
                rootkey,
            ).fetchone()
            data = old["data"] if old else {"id": trace_id, "errors": [], "retries": []}
            if kind in ("trace.started", "trace.finished"):
                data.update(p)
                data["id"] = trace_id
                if kind == "trace.started":
                    data.setdefault("status", "running")
            elif kind == "trace.error":
                data["errors"].append(dict(p, event_version=v))
            elif kind == "trace.retry":
                data["retries"].append(dict(p, event_version=v))
            elif kind.startswith("trace.span."):
                prev = conn.execute(
                    "SELECT data FROM trace_spans WHERE user_id=%s AND scope=%s AND id=%s",
                    key,
                ).fetchone()
                span = prev["data"] if prev else {"id": e["entity_id"]}
                span.update(p)
                conn.execute(
                    "INSERT INTO trace_spans VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,scope,id) DO UPDATE SET data=EXCLUDED.data,version=EXCLUDED.version",
                    (*key, trace_id, Jsonb(span), v),
                )
            conn.execute(
                "INSERT INTO trace_roots VALUES(%s,%s,%s,%s,%s) ON CONFLICT(user_id,scope,id) DO UPDATE SET data=EXCLUDED.data,version=EXCLUDED.version",
                (*rootkey, Jsonb(data), v),
            )
        if kind == "skill.invoked":
            conn.execute(
                "INSERT INTO skill_calls VALUES(%s,%s,%s,%s,%s) ON CONFLICT(user_id,scope,id) DO UPDATE SET data=EXCLUDED.data,version=EXCLUDED.version",
                (*key, Jsonb(p), v),
            )
            if enqueue and p.get("status") in ("error", "failed", "failure"):
                self.enqueue(
                    e["user_id"],
                    e["scope"],
                    "recommend",
                    {"skill_name": p["skill_name"]},
                    f"recommend:{v}",
                    conn,
                )

    def sync(self, user, after=0, limit=1000, scope=None):
        clause = " AND scope=%s" if scope is not None else ""
        args = [user, after] + ([scope] if scope is not None else []) + [limit + 1]
        with self.connection() as c:
            rows = c.execute(
                "SELECT * FROM events WHERE user_id=%s AND id>%s"
                + clause
                + " ORDER BY id LIMIT %s",
                args,
            ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        for r in rows:
            r.pop("request_hash", None)
        return {
            "events": rows,
            "next_version": rows[-1]["id"] if rows else after,
            "has_more": more,
        }

    def memories(self, user, scope, deleted=False):
        with self.connection() as c:
            return c.execute(
                "SELECT * FROM memory_items WHERE user_id=%s AND scope=%s AND (%s OR NOT deleted) ORDER BY version DESC LIMIT 1000",
                (user, scope, deleted),
            ).fetchall()

    def memory(self, user, scope, mid):
        with self.connection() as c:
            return c.execute(
                "SELECT * FROM memory_items WHERE user_id=%s AND scope=%s AND id=%s",
                (user, scope, mid),
            ).fetchone()

    def enqueue(self, user, scope, kind, payload, dedupe, conn=None):
        if conn is None:
            with self.connection() as c:
                return self.enqueue(user, scope, kind, payload, dedupe, c)
        jid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{user}:{dedupe}"))
        row = conn.execute(
            "INSERT INTO jobs(id,user_id,scope,kind,payload,dedupe) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,dedupe) DO UPDATE SET dedupe=EXCLUDED.dedupe RETURNING *",
            (jid, user, scope, kind, Jsonb(payload), dedupe),
        ).fetchone()
        return row

    def rebuild(self, user):
        with self.connection() as c:
            c.execute("SELECT version FROM event_clock WHERE singleton=TRUE FOR UPDATE")
            for table in (
                "memory_items",
                "trace_roots",
                "trace_spans",
                "skill_calls",
                "user_prefs",
            ):
                c.execute(f"DELETE FROM {table} WHERE user_id=%s", (user,))
            rows = c.execute(
                "SELECT * FROM events WHERE user_id=%s ORDER BY id", (user,)
            ).fetchall()
            for e in rows:
                self.project(c, e, enqueue=False)
            for m in c.execute("SELECT * FROM memory_items WHERE user_id=%s", (user,)).fetchall():
                self.enqueue(
                    user,
                    m["scope"],
                    "vector",
                    {"memory_id": m["id"], "version": m["version"]},
                    f"rebuild:{m['scope']}:{m['id']}:{uuid.uuid4()}",
                    c,
                )
        return {"events_replayed": len(rows)}

    def import_semantic(self, user, scope, events):
        """Commit semantic state transitions and their deduplication state together.

        Content hashes alone cannot represent A -> B -> A. A durable revision
        distinguishes the second A while unchanged polls remain idempotent.
        """
        with self.connection() as c:
            c.execute("SELECT version FROM event_clock WHERE singleton=TRUE FOR UPDATE")
            pending = []
            for incoming in events:
                e = dict(incoming)
                identity = json.dumps(
                    [
                        user,
                        scope,
                        e["metadata"].get("source"),
                        e["metadata"].get("source_trace_id"),
                        e["event_type"],
                        e["entity_id"],
                        e["payload"].get("span_id"),
                    ],
                    ensure_ascii=False,
                )
                name = "semantic:" + hashlib.sha256(identity.encode()).hexdigest()
                snapshot = hashlib.sha256(
                    json.dumps(
                        [e["payload"], e["metadata"]], sort_keys=True, ensure_ascii=False
                    ).encode()
                ).hexdigest()
                old = c.execute("SELECT data FROM adapter_state WHERE name=%s", (name,)).fetchone()
                state = old["data"] if old else {"revision": 0}
                if state.get("snapshot") == snapshot:
                    continue
                state = {"revision": state["revision"] + 1, "snapshot": snapshot}
                e["event_id"] = str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"{name}:{state['revision']}:{snapshot}")
                )
                pending.append(e)
                c.execute(
                    "INSERT INTO adapter_state VALUES(%s,%s) ON CONFLICT(name) DO UPDATE SET data=EXCLUDED.data",
                    (name, Jsonb(state)),
                )
            for start in range(0, len(pending), 500):
                self.append(user, scope, pending[start : start + 500], c)
            return {"accepted": len(pending)}

    def traces(self, user, scope):
        with self.connection() as c:
            return [
                dict(r["data"], version=r["version"], scope=scope)
                for r in c.execute(
                    "SELECT * FROM trace_roots WHERE user_id=%s AND scope=%s ORDER BY version DESC LIMIT 200",
                    (user, scope),
                ).fetchall()
            ]

    def trace(self, user, scope, tid):
        with self.connection() as c:
            row = c.execute(
                "SELECT data FROM trace_roots WHERE user_id=%s AND scope=%s AND id=%s",
                (user, scope, tid),
            ).fetchone()
            if not row:
                return None
            spans = [
                r["data"]
                for r in c.execute(
                    "SELECT data FROM trace_spans WHERE user_id=%s AND scope=%s AND trace_id=%s ORDER BY version",
                    (user, scope, tid),
                ).fetchall()
            ]
            errors = row["data"].get("errors", [])
            for span in spans:
                span["errors"] = [e for e in errors if e.get("span_id") == span["id"]]
            return {"trace": row["data"], "spans": spans, "errors": errors}

    def analysis(self, user, scope, skill=None):
        with self.connection() as c:
            calls = [
                dict(r["data"], version=r["version"])
                for r in c.execute(
                    "SELECT * FROM skill_calls WHERE user_id=%s AND scope=%s",
                    (user, scope),
                ).fetchall()
            ]
            roots = c.execute(
                "SELECT data FROM trace_roots WHERE user_id=%s AND scope=%s",
                (user, scope),
            ).fetchall()
            injections = c.execute(
                "SELECT id,payload FROM events WHERE user_id=%s AND scope=%s AND event_type='memory.injected' ORDER BY id",
                (user, scope),
            ).fetchall()
        error_types = {
            e.get("span_id"): e.get("error_type", "unknown")
            for r in roots
            for e in r["data"].get("errors", [])
            if e.get("span_id")
        }
        groups = defaultdict(list)
        running = Counter()
        for p in calls:
            if not skill or p["skill_name"] == skill:
                if p.get("status") in (
                    "success",
                    "ok",
                    "completed",
                    "error",
                    "failed",
                    "failure",
                ):
                    groups[p["skill_name"]].append(p)
                else:
                    running[p["skill_name"]] += 1
        result = []
        for name, items in sorted(groups.items()):
            successes = sum(p.get("status") in ("success", "ok", "completed") for p in items)
            errors = Counter(
                p.get("error_type")
                or error_types.get(p.get("span_id", p.get("invocation_id")), "unknown")
                for p in items
                if p.get("status") in ("error", "failed", "failure")
            )
            evaluation = {
                "conclusion": "insufficient_evidence",
                "reason": "尚无可对比的注入记录",
                "before_count": 0,
                "after_count": 0,
            }
            relevant = [
                i for i in injections if i["payload"].get("context", {}).get("skill_name") == name
            ]
            if relevant:
                injection = relevant[-1]
                before = [p for p in items if p["version"] < injection["id"]]
                after = [p for p in items if p["version"] > injection["id"]]

                def rate(xs):
                    return (
                        sum(p.get("status") in ("error", "failed", "failure") for p in xs) / len(xs)
                        if xs
                        else None
                    )

                evaluation = {
                    "injection_version": injection["id"],
                    "before_count": len(before),
                    "after_count": len(after),
                    "before_failure_rate": rate(before),
                    "after_failure_rate": rate(after),
                    "conclusion": "insufficient_evidence",
                    "reason": "每组至少 5 次执行；前后相关性不代表因果",
                }
                if len(before) >= 5 and len(after) >= 5:
                    evaluation["conclusion"] = (
                        "retain_candidate" if rate(after) < rate(before) else "review_candidate"
                    )
            result.append(
                {
                    "skill_name": name,
                    "calls": len(items),
                    "in_progress": running[name],
                    "success_rate": successes / len(items),
                    "avg_duration_ms": sum(p.get("duration_ms", 0) for p in items) / len(items),
                    "avg_tokens": sum(p.get("tokens", 0) for p in items) / len(items),
                    "top_errors": [{"error_type": k, "count": v} for k, v in errors.most_common(5)],
                    "evaluation": evaluation,
                }
            )
        return {"skills": result}

    def status(self, user, scope=None):
        with self.connection() as c:
            clause = " AND scope=%s" if scope is not None else ""
            args = (user, scope) if scope is not None else (user,)
            return {
                key: c.execute(
                    f"SELECT count(*) AS n FROM {table} WHERE user_id=%s" + clause + extra,
                    args,
                ).fetchone()["n"]
                for key, table, extra in (
                    ("events", "events", ""),
                    ("memories", "memory_items", " AND NOT deleted"),
                    ("traces", "trace_roots", ""),
                    (
                        "pending_jobs",
                        "jobs",
                        " AND status IN ('pending','running','retry')",
                    ),
                )
            }
