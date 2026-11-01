import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import (
    TransportSecuritySettings as _TransportSettings,
)
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response

from .config import settings
from .core import Conflict, Core, InvalidEvent
from .semantic import Semantic
from .service import Service, new_event

s = settings()
core = Core(s.database_url)
service = Service(core, Semantic(s))
mcp = FastMCP(
    "Memory OS",
    streamable_http_path="/",
    stateless_http=True,
    transport_security=_TransportSettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=s.mcp_allowed_hosts.split(","),
        allowed_origins=["http://localhost:*", "http://127.0.0.1:*"],
    ),
)


def identity(request):
    supplied = request.headers.get("authorization", "")
    if not supplied.startswith("Bearer "):
        raise HTTPException(401, "Bearer API key required")
    token = supplied[7:]
    for key, user in s.keys().items():
        if secrets.compare_digest(token, key):
            return user
    raise HTTPException(401, "Invalid API key")


def user(request: Request):
    return identity(request)


def mcp_user():
    request = mcp.get_context().request_context.request
    if request is None:
        raise ValueError("HTTP authentication context unavailable")
    return identity(request)


@asynccontextmanager
async def lifespan(app):
    if not s.keys():
        raise RuntimeError("Set API_KEY before starting the service")
    core.initialize()
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Memory OS", version="0.1.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_handler(request, exc):
    # Never echo submitted credentials in validation errors.
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()
            ]
        },
    )


@app.exception_handler(Conflict)
async def conflict_handler(request, exc):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(InvalidEvent)
async def invalid_handler(request, exc):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.middleware("http")
async def headers_and_auth(request, call_next):
    if request.url.path.startswith("/mcp"):
        try:
            identity(request)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


class ScopeBody(BaseModel):
    scope: str = Field(default="default", min_length=1, max_length=256)


class MemoryBody(ScopeBody):
    content: str = Field(min_length=1, max_length=100000)
    tags: list[str] = Field(default_factory=list, max_length=100)
    infer: bool = True
    request_id: str | None = None
    source_trace_id: str | None = None


class UpdateBody(ScopeBody):
    content: str = Field(min_length=1, max_length=100000)
    tags: list[str] | None = None
    reason: str = "explicit edit"
    request_id: str | None = None


class EventBatch(ScopeBody):
    events: list[dict] = Field(min_length=1, max_length=500)


class RecallBody(ScopeBody):
    query: str = Field(min_length=1, max_length=10000)
    top_k: int = Field(default=5, ge=1, le=100)
    tags: list[str] | None = None


class InjectBody(ScopeBody):
    trigger: str = "session.start"
    query: str = ""
    skill_name: str | None = None
    budget: int = Field(default=500, ge=0, le=500)
    token_budget: int | None = Field(default=None, ge=0, le=500)
    channel: str = "api"
    source_trace_id: str | None = None
    context: dict = Field(default_factory=dict)


class RecommendBody(ScopeBody):
    skill_name: str


class CaptureEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=256)
    type: str = Field(min_length=1, max_length=128)
    timestamp: str = Field(min_length=1, max_length=64)
    body: dict


class CaptureBatch(ScopeBody):
    model_config = ConfigDict(extra="forbid")
    source: str = "codex"
    source_session_id: str = Field(min_length=1, max_length=256)
    batch_id: str = Field(min_length=1, max_length=256)
    events: list[CaptureEvent] = Field(max_length=500)
    turn_complete: bool = False


def admin(request: Request):
    uid = identity(request)
    if not s.api_key or not secrets.compare_digest(
        request.headers.get("authorization", ""), f"Bearer {s.api_key}"
    ):
        raise HTTPException(403, "Only the deployment admin can manage model settings")
    return uid


class ModelEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(min_length=1, max_length=2000)
    model: str = Field(min_length=1, max_length=256)
    api_key: str | None = Field(default=None, max_length=8192)


class EmbeddingEndpoint(ModelEndpoint):
    dimensions: int = Field(ge=1, le=65536)
    send_dimensions: bool = False


class ModelSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    llm: ModelEndpoint
    embedding: EmbeddingEndpoint
    reindex: bool = False


class TestConnectionBody(BaseModel):
    target: str = Field(pattern="^(llm|embedding)$")


@app.get("/api/v1/settings")
def get_settings(uid: str = Depends(admin)):
    from .runtime import public_settings

    return public_settings(s)


@app.put("/api/v1/settings")
def put_settings(body: ModelSettingsBody, uid: str = Depends(admin)):
    from .runtime import save_settings

    return save_settings(s, body.model_dump(exclude_none=True))


@app.post("/api/v1/settings/test")
def check_settings(body: TestConnectionBody, uid: str = Depends(admin)):
    from .runtime import test_connection

    return test_connection(s, body.target)


def do_recall(uid, b):
    try:
        return service.recall(uid, b.scope, b.query, b.top_k, b.tags)
    except (InvalidEvent, Conflict):
        raise
    except Exception:
        raise HTTPException(
            503,
            "Semantic search unavailable; check embedding configuration, index jobs and Qdrant",
        )


def do_inject(uid, b):
    try:
        return service.inject(
            uid,
            b.scope,
            b.trigger,
            b.query or b.context.get("query", ""),
            b.skill_name or b.context.get("skill_name"),
            b.token_budget if b.token_budget is not None else b.budget,
            b.channel,
            b.source_trace_id or b.context.get("trace_id"),
        )
    except (InvalidEvent, Conflict):
        raise
    except Exception:
        raise HTTPException(
            503, "Injection retrieval unavailable; check model configuration and Qdrant"
        )


@app.get("/health")
def health():
    with core.connection() as c:
        c.execute("SELECT 1")
    return {"status": "ok", "version": "0.1.0"}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/api/v1/status")
def status(scope: str | None = None, uid: str = Depends(user)):
    return core.status(uid, scope)


@app.post("/api/v1/events")
def append(b: EventBatch, uid: str = Depends(user)):
    return core.append(uid, b.scope, b.events)


@app.get("/api/v1/events/sync")
def sync(
    after_version: int = Query(0, ge=0),
    limit: int = Query(1000, ge=1, le=1000),
    scope: str | None = None,
    uid: str = Depends(user),
):
    return core.sync(uid, after_version, limit, scope)


@app.get("/api/v1/memories")
def memories(scope: str = "default", uid: str = Depends(user)):
    return {"items": core.memories(uid, scope)}


@app.post("/api/v1/memories")
def api_remember(b: MemoryBody, uid: str = Depends(user)):
    return service.remember(
        uid, b.scope, b.content, b.tags, b.infer, b.request_id, b.source_trace_id
    )


@app.patch("/api/v1/memories/{mid}")
def update(mid: str, b: UpdateBody, uid: str = Depends(user)):
    payload = {"new_content": b.content, "reason": b.reason}
    if b.tags is not None:
        payload["tags"] = b.tags
    return core.append(
        uid, b.scope, [new_event("memory.updated", mid, payload, event_id=b.request_id)]
    )


@app.delete("/api/v1/memories/{mid}")
def api_forget(
    mid: str,
    scope: str = "default",
    request_id: str | None = None,
    uid: str = Depends(user),
):
    return core.append(
        uid,
        scope,
        [
            new_event(
                "memory.deleted",
                mid,
                {"reason": "explicit forget"},
                event_id=request_id,
            )
        ],
    )


@app.post("/api/v1/memories/recall")
@app.post("/api/v1/recall")
def api_recall(b: RecallBody, uid: str = Depends(user)):
    return do_recall(uid, b)


@app.post("/api/v1/memories/inject")
@app.post("/api/v1/inject")
def api_inject(b: InjectBody, uid: str = Depends(user)):
    return do_inject(uid, b)


@app.get("/api/v1/jobs/{jid}")
def job(jid: uuid.UUID, uid: str = Depends(user)):
    with core.connection() as c:
        row = c.execute(
            "SELECT id,kind,status,attempts,error,result,created_at FROM jobs WHERE id=%s AND user_id=%s",
            (jid, uid),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Job not found")
        return row


@app.post("/api/v1/jobs/{jid}/retry")
def retry(jid: uuid.UUID, uid: str = Depends(user)):
    with core.connection() as c:
        row = c.execute(
            "UPDATE jobs SET status='pending',attempts=0,error=NULL,available_at=now() WHERE id=%s AND user_id=%s AND status='failed' RETURNING id,status",
            (jid, uid),
        ).fetchone()
        if not row:
            raise HTTPException(409, "Only failed jobs can be retried")
        return row


@app.get("/api/v1/traces")
def traces(scope: str = "default", uid: str = Depends(user)):
    return {"items": core.traces(uid, scope)}


@app.post("/api/v1/capture/batches")
def capture_batch(body: CaptureBatch, uid: str = Depends(user)):
    from .capture import ingest_batch

    return ingest_batch(core, uid, body.model_dump())


@app.get("/api/v1/capture/sessions")
def capture_sessions(scope: str = "default", uid: str = Depends(user)):
    from .capture import list_sessions

    return {"items": list_sessions(core, uid, scope)}


@app.get("/api/v1/capture/scopes")
def capture_scopes(uid: str = Depends(user)):
    with core.connection() as conn:
        rows = conn.execute(
            "SELECT scope FROM capture_sessions WHERE user_id=%s UNION SELECT scope FROM memory_items WHERE user_id=%s ORDER BY scope",
            (uid, uid),
        ).fetchall()
    return {"items": [row["scope"] for row in rows]}


@app.get("/api/v1/capture/sessions/{sid}")
def capture_session(
    sid: uuid.UUID,
    scope: str = "default",
    after_seq: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    uid: str = Depends(user),
):
    from .capture import session_detail

    detail = session_detail(core, uid, scope, sid, after_seq, limit)
    if not detail:
        raise HTTPException(404, "Capture session not found")
    return detail


@app.get("/api/v1/capture/reviews")
def capture_reviews(scope: str = "default", uid: str = Depends(user)):
    from .capture import list_reviews

    return {"items": list_reviews(core, uid, scope)}


@app.get("/api/v1/capture/reviews/{rid}")
def capture_review(rid: uuid.UUID, scope: str = "default", uid: str = Depends(user)):
    from .capture import review_detail

    detail = review_detail(core, uid, scope, rid)
    if not detail:
        raise HTTPException(404, "Capture review not found")
    return detail


@app.post("/api/v1/capture/candidates/{cid}/approve")
def approve_capture_candidate(cid: uuid.UUID, body: ScopeBody, uid: str = Depends(user)):
    from .capture import approve_candidate

    result = approve_candidate(core, uid, body.scope, cid)
    if not result:
        raise HTTPException(404, "Capture candidate not found")
    return result


@app.get("/api/v1/traces/{tid}")
def trace(tid: str, scope: str = "default", uid: str = Depends(user)):
    result = core.trace(uid, scope, tid)
    if not result:
        raise HTTPException(404, "Trace not found")
    return result


@app.get("/api/v1/analysis")
def analysis(scope: str = "default", skill: str | None = None, uid: str = Depends(user)):
    return core.analysis(uid, scope, skill)


@app.post("/api/v1/analysis/recommend")
def recommend(b: RecommendBody, uid: str = Depends(user)):
    return service.recommend(uid, b.scope, b.skill_name)


@app.post("/api/v1/admin/rebuild")
def rebuild(uid: str = Depends(user)):
    return core.rebuild(uid)


@mcp.tool()
def remember(
    content: str,
    tags: list[str] | None = None,
    scope: str = "default",
    infer: bool = True,
    request_id: str | None = None,
) -> dict:
    """Extract durable memories. Poll the returned job_id; extraction is asynchronous."""
    b = MemoryBody(
        content=content,
        tags=tags or [],
        scope=scope,
        infer=infer,
        request_id=request_id,
    )
    return service.remember(mcp_user(), b.scope, b.content, b.tags, b.infer, b.request_id)


@mcp.tool()
def recall(query: str, scope: str = "default", top_k: int = 5) -> dict:
    """Semantic search within the authenticated user's project scope."""
    return do_recall(mcp_user(), RecallBody(query=query, scope=scope, top_k=top_k))


@mcp.tool()
def forget(memory_id: str, scope: str = "default") -> dict:
    """Soft delete a memory; historical events remain for auditing."""
    return core.append(
        mcp_user(),
        scope,
        [new_event("memory.deleted", memory_id, {"reason": "mcp forget"})],
    )


@mcp.tool()
def inject(
    scope: str = "default",
    trigger: str = "session.start",
    query: str = "",
    skill_name: str | None = None,
    budget: int = 500,
) -> dict:
    """Return relevant memories as untrusted context with a bounded token budget."""
    return do_inject(
        mcp_user(),
        InjectBody(
            scope=scope,
            trigger=trigger,
            query=query,
            skill_name=skill_name,
            budget=budget,
            channel="mcp",
        ),
    )


@mcp.tool(name="trace_get")
def trace_get(trace_id: str, scope: str = "default") -> dict:
    """Semantic trace summary; raw spans live in Langfuse."""
    return core.trace(mcp_user(), scope, trace_id) or {"error": "not found"}


@mcp.tool(name="capture_report")
def capture_report(
    scope: str,
    session_id: str,
    batch_id: str,
    events: list[dict],
    turn_complete: bool = False,
) -> dict:
    """Supplement Codex execution evidence with explicitly reported events.

    This is a model-supplied report, not automatic complete execution capture
    and not a durable memory write. Each event needs source_id, type, timestamp
    (ISO8601), and body. Reuse batch_id/source_id on retries with identical
    content. turn_complete schedules review of new evidence; memory candidates
    still require human approval. Never report this capture_report MCP call or
    its result as another captured event, to prevent recursive self-collection.
    """
    from fastapi.encoders import jsonable_encoder

    from .capture import ingest_batch

    uid = mcp_user()
    body = CaptureBatch(
        scope=scope,
        source="codex",
        source_session_id=session_id,
        batch_id=batch_id,
        events=events,
        turn_complete=turn_complete,
    )
    return jsonable_encoder(ingest_batch(core, uid, body.model_dump()))


@mcp.tool(name="trace_analyze")
def trace_analyze(scope: str = "default", skill: str | None = None) -> dict:
    return core.analysis(mcp_user(), scope, skill)


@mcp.tool(name="events_sync")
def events_sync(after_version: int = 0, limit: int = 1000, scope: str | None = None) -> dict:
    if after_version < 0 or not 1 <= limit <= 1000:
        raise ValueError("invalid cursor or limit")
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(core.sync(mcp_user(), after_version, limit, scope))


@mcp.tool(name="job_get")
def job_get(job_id: str) -> dict:
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(job(uuid.UUID(job_id), mcp_user()))


app.mount("/mcp", mcp.streamable_http_app())
