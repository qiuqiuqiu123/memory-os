"""Real OpenAI-compatible embeddings + Qdrant index. No lexical fallback."""

import uuid

from openai import OpenAI
from qdrant_client import QdrantClient, models


class Semantic:
    def __init__(self, settings, dynamic=True):
        self.s = settings
        self.dynamic = dynamic
        self.q = QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
            timeout=20,
            check_compatibility=False,
        )

    def embed(self, text):
        if not self.s.embedding_api_key or not self.s.embedding_model:
            raise RuntimeError("EMBEDDING_API_KEY and EMBEDDING_MODEL are required")
        client = OpenAI(
            api_key=self.s.embedding_api_key,
            base_url=self.s.embedding_base_url,
            timeout=45,
            max_retries=1,
        )
        args = {"model": self.s.embedding_model, "input": text}
        if self.s.embedding_send_dimensions:
            args["dimensions"] = self.s.embedding_dimensions
        try:
            vector = client.embeddings.create(**args).data[0].embedding
        finally:
            client.close()
        if len(vector) != self.s.embedding_dimensions:
            raise RuntimeError("Embedding dimensions differ from EMBEDDING_DIMENSIONS")
        return vector

    def ensure_collection(self):
        if not self.q.collection_exists(self.s.qdrant_collection):
            try:
                self.q.create_collection(
                    self.s.qdrant_collection,
                    vectors_config=models.VectorParams(
                        size=self.s.embedding_dimensions,
                        distance=models.Distance.COSINE,
                    ),
                )
            except Exception:
                if not self.q.collection_exists(self.s.qdrant_collection):
                    raise
        config = self.q.get_collection(self.s.qdrant_collection).config.params.vectors
        if (
            not isinstance(config, models.VectorParams)
            or config.size != self.s.embedding_dimensions
            or config.distance != models.Distance.COSINE
        ):
            raise RuntimeError(
                "Vector collection space does not match model settings; create a new collection and reindex"
            )

    @staticmethod
    def point_id(user, scope, mid):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"memory-os:{user}:{scope}:{mid}"))

    def index(self, m):
        if self.dynamic:
            from .runtime import resolve_settings

            active = resolve_settings(self.s, m["user_id"])
            if active != self.s:
                scoped = Semantic(active, dynamic=False)
                try:
                    return scoped.index(m)
                finally:
                    scoped.q.close()
        self.ensure_collection()
        pid = self.point_id(m["user_id"], m["scope"], m["id"])
        if m["deleted"]:
            self.q.delete(self.s.qdrant_collection, models.PointIdsList(points=[pid]), wait=True)
            return
        vector = self.embed(m["content"])
        self.q.upsert(
            self.s.qdrant_collection,
            [
                models.PointStruct(
                    id=pid,
                    vector=vector,
                    payload={
                        "user_id": m["user_id"],
                        "scope": m["scope"],
                        "memory_id": m["id"],
                        "version": m["version"],
                        "tags": m["tags"],
                    },
                )
            ],
            wait=True,
        )

    def search(self, user, scope, query, top_k, tags=None):
        if self.dynamic:
            from .runtime import resolve_settings

            active = resolve_settings(self.s, user)
            if active != self.s:
                scoped = Semantic(active, dynamic=False)
                try:
                    return scoped.search(user, scope, query, top_k, tags)
                finally:
                    scoped.q.close()
        vector = self.embed(query)
        self.ensure_collection()
        must = [
            models.FieldCondition(key="user_id", match=models.MatchValue(value=user)),
            models.FieldCondition(key="scope", match=models.MatchValue(value=scope)),
        ]
        if tags:
            must.append(models.FieldCondition(key="tags", match=models.MatchAny(any=tags)))
        return self.q.query_points(
            self.s.qdrant_collection,
            query=vector,
            query_filter=models.Filter(must=must),
            limit=top_k,
        ).points
