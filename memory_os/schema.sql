CREATE TABLE IF NOT EXISTS event_clock (singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton), version BIGINT NOT NULL);
INSERT INTO event_clock VALUES(TRUE,0) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS events (
 id BIGINT PRIMARY KEY, event_id UUID NOT NULL, user_id TEXT NOT NULL, scope TEXT NOT NULL,
 event_type TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
 payload JSONB NOT NULL, metadata JSONB NOT NULL DEFAULT '{}', request_hash TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), UNIQUE(user_id,event_id)
);
CREATE INDEX IF NOT EXISTS events_user_version ON events(user_id,id);
CREATE INDEX IF NOT EXISTS events_entity ON events(user_id,scope,entity_type,entity_id,id);
CREATE OR REPLACE FUNCTION prevent_event_mutation() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'events are append-only'; END; $$ LANGUAGE plpgsql;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='events_immutable') THEN
 CREATE TRIGGER events_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON events FOR EACH STATEMENT EXECUTE FUNCTION prevent_event_mutation();
 END IF;
END $$;
CREATE TABLE IF NOT EXISTS memory_items (
 user_id TEXT NOT NULL, scope TEXT NOT NULL, id TEXT NOT NULL, content TEXT NOT NULL,
 tags JSONB NOT NULL DEFAULT '[]', source_trace_id TEXT, deleted BOOLEAN NOT NULL DEFAULT FALSE,
 version BIGINT NOT NULL, metadata JSONB NOT NULL DEFAULT '{}', PRIMARY KEY(user_id,scope,id)
);
CREATE TABLE IF NOT EXISTS trace_roots (
 user_id TEXT NOT NULL,scope TEXT NOT NULL,id TEXT NOT NULL,data JSONB NOT NULL,version BIGINT NOT NULL,
 PRIMARY KEY(user_id,scope,id)
);
CREATE TABLE IF NOT EXISTS trace_spans (
 user_id TEXT NOT NULL,scope TEXT NOT NULL,id TEXT NOT NULL,trace_id TEXT NOT NULL,data JSONB NOT NULL,version BIGINT NOT NULL,
 PRIMARY KEY(user_id,scope,id)
);
CREATE TABLE IF NOT EXISTS skill_calls (
 user_id TEXT NOT NULL,scope TEXT NOT NULL,id TEXT NOT NULL,data JSONB NOT NULL,version BIGINT NOT NULL,
 PRIMARY KEY(user_id,scope,id)
);
CREATE TABLE IF NOT EXISTS user_prefs (
 user_id TEXT NOT NULL,scope TEXT NOT NULL,id TEXT NOT NULL,data JSONB NOT NULL,version BIGINT NOT NULL,
 PRIMARY KEY(user_id,scope,id)
);
CREATE TABLE IF NOT EXISTS jobs (
 id UUID PRIMARY KEY,user_id TEXT NOT NULL,scope TEXT NOT NULL,kind TEXT NOT NULL,payload JSONB NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,error TEXT,result JSONB,
 available_at TIMESTAMPTZ NOT NULL DEFAULT now(),lease_until TIMESTAMPTZ,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 dedupe TEXT NOT NULL,UNIQUE(user_id,dedupe)
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status,available_at);
CREATE TABLE IF NOT EXISTS adapter_state (name TEXT PRIMARY KEY,data JSONB NOT NULL);
CREATE TABLE IF NOT EXISTS schema_versions (version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now());
INSERT INTO schema_versions(version) VALUES (1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS model_settings (
 singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
 revision INTEGER NOT NULL, encrypted TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO schema_versions(version) VALUES (2) ON CONFLICT DO NOTHING;
