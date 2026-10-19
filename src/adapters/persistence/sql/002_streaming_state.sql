-- Shared streaming state, so SSE/WebSocket streams, webhook de-duplication and
-- WebSocket ticket replay protection work across containers.
-- Applied idempotently at startup together with 001 (see postgres.ensure_schema).

-- Event log. seq is assigned under an advisory lock in PostgresEventBus.publish,
-- so seq order == commit order and a reader's "seq > cursor" can never skip a
-- row that commits late. Old rows are pruned by the bus (retention + row cap).
CREATE TABLE IF NOT EXISTS events (
    seq   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    topic text        NOT NULL,
    type  text        NOT NULL,
    data  jsonb       NOT NULL,
    ts    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_topic_seq_idx ON events (topic, seq);
CREATE INDEX IF NOT EXISTS events_ts_idx ON events (ts);

-- Webhook idempotency: the primary key makes "first delivery wins" atomic.
CREATE TABLE IF NOT EXISTS webhook_events (
    provider   text        NOT NULL,
    webhook_id text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, webhook_id)
);
CREATE INDEX IF NOT EXISTS webhook_events_created_idx ON webhook_events (created_at);

-- WebSocket ticket replay protection: a ticket id can be consumed once.
CREATE TABLE IF NOT EXISTS ws_ticket_uses (
    jti        text        PRIMARY KEY,
    expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ws_ticket_uses_expires_idx ON ws_ticket_uses (expires_at);

-- Keep these out of Supabase's REST API (same reasoning as items).
ALTER TABLE events         ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE ws_ticket_uses ENABLE ROW LEVEL SECURITY;
