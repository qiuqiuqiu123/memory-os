"""Durable leased jobs; retries are visible, never reported as successful mocks."""

import logging
import time
import uuid

from psycopg.types.json import Jsonb

from .config import settings
from .core import Core
from .semantic import Semantic
from .service import Service, new_event

log = logging.getLogger("memory_os.worker")


def claim(core):
    with core.connection() as c:
        return c.execute("""UPDATE jobs SET status='running', attempts=attempts+1,lease_until=now()+interval '5 minutes'
        WHERE id=(SELECT id FROM jobs WHERE ((status IN ('pending','retry') AND available_at<=now())
        OR (status='running' AND lease_until<now())) ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *""").fetchone()


def process(core, semantic, job):
    from .integrations import Mem0Backend

    user = job["user_id"]
    scope = job["scope"]
    p = job["payload"]
    if job["kind"] == "extract":
        results = Mem0Backend().extract(p["content"], user, scope)
        events = []
        ids = []
        for i, m in enumerate(results):
            content = m.get("content")
            if not content:
                continue
            mid = str(uuid.uuid5(job["id"], str(i)))
            ids.append(mid)
            events.append(
                new_event(
                    "memory.created",
                    mid,
                    {
                        "content": content,
                        "tags": p.get("tags", []),
                        "source_trace_id": p.get("source_trace_id"),
                    },
                    event_id=str(uuid.uuid5(job["id"], f"event:{i}")),
                )
            )
        with core.connection() as c:
            active = c.execute(
                "SELECT attempts,status FROM jobs WHERE id=%s FOR UPDATE", (job["id"],)
            ).fetchone()
            if active["attempts"] != job["attempts"] or active["status"] != "running":
                return
            if events:
                core.append(user, scope, events, c)
            c.execute(
                "UPDATE jobs SET status='completed',result=%s,error=NULL,lease_until=NULL WHERE id=%s",
                (Jsonb({"memory_ids": ids}), job["id"]),
            )
    elif job["kind"] == "vector":
        # Hold an advisory lock across the network call, serializing all jobs for this memory.
        # Reread the current projection: a delayed old job indexes the latest version.
        with core.connection() as c:
            c.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"vector:{user}:{scope}:{p['memory_id']}",),
            )
            active = c.execute(
                "SELECT attempts,status FROM jobs WHERE id=%s FOR UPDATE", (job["id"],)
            ).fetchone()
            if active["attempts"] != job["attempts"] or active["status"] != "running":
                return
            m = c.execute(
                "SELECT * FROM memory_items WHERE user_id=%s AND scope=%s AND id=%s",
                (user, scope, p["memory_id"]),
            ).fetchone()
            if m:
                semantic.index(m)
            c.execute(
                "UPDATE jobs SET status='completed',error=NULL,lease_until=NULL WHERE id=%s AND attempts=%s",
                (job["id"], job["attempts"]),
            )
    elif job["kind"] == "capture_review":
        from .capture import process_review

        process_review(core, job)
    elif job["kind"] == "recommend":
        service = Service(core, semantic)
        stats = core.analysis(user, scope, p["skill_name"])["skills"]
        if stats and stats[0]["calls"] >= 5 and stats[0]["success_rate"] < 0.8:
            service.recommend(user, scope, p["skill_name"])
        with core.connection() as c:
            c.execute(
                "UPDATE jobs SET status='completed',error=NULL,lease_until=NULL WHERE id=%s",
                (job["id"],),
            )
    else:
        raise RuntimeError(f"unknown job kind {job['kind']}")


def run_once(core, semantic):
    job = claim(core)
    if not job:
        return False
    try:
        process(core, semantic, job)
    except Exception as exc:
        # Do not persist provider error bodies, which may echo secret headers or private prompts.
        message = (
            type(exc).__name__
            + ": backend operation failed; check model configuration and connectivity"
        )
        with core.connection() as c:
            c.execute(
                "UPDATE jobs SET status=%s,error=%s,lease_until=NULL,available_at=now()+(%s * interval '1 second') WHERE id=%s AND attempts=%s AND status='running'",
                (
                    "failed" if job["attempts"] >= 5 else "retry",
                    message,
                    min(60, 2 ** job["attempts"]),
                    job["id"],
                    job["attempts"],
                ),
            )
        log.warning(
            "job %s failed (%s), attempt %s",
            job["id"],
            type(exc).__name__,
            job["attempts"],
        )
    return True


def main():
    logging.basicConfig(level=logging.INFO)
    s = settings()
    core = Core(s.database_url)
    core.initialize()
    semantic = Semantic(s)
    service = Service(core, semantic)
    last_poll = 0
    while True:
        did_work = run_once(core, semantic)
        if s.langfuse_public_key and s.langfuse_secret_key and time.monotonic() - last_poll >= 30:
            try:
                from .integrations import LangfuseAdapter

                adapter = LangfuseAdapter()
                for user, scope, events in adapter.poll():
                    core.import_semantic(user, scope, events)
                # Conservative recommendations are evidence-backed and idempotent per statistics snapshot.
                for skill in core.analysis(s.langfuse_user_id, s.langfuse_scope)["skills"]:
                    if skill["calls"] >= 5 and skill["success_rate"] < 0.8:
                        service.recommend(s.langfuse_user_id, s.langfuse_scope, skill["skill_name"])
            except Exception as exc:
                log.warning("Langfuse adapter failed (%s)", type(exc).__name__)
            last_poll = time.monotonic()
        if not did_work:
            time.sleep(s.worker_interval)


if __name__ == "__main__":
    main()
