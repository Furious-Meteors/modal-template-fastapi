-- Items table. Applied automatically at startup by PostgresItemRepository
-- (idempotent), and safe to run by hand or via `supabase db push`.
--
-- seq gives list() a stable, insertion-ordered result.
-- The unique index is the source of truth for name uniqueness: it matches the
-- service's case- and whitespace-insensitive check and closes the
-- check-then-write race between containers.
CREATE TABLE IF NOT EXISTS items (
    seq         bigint GENERATED ALWAYS AS IDENTITY,
    id          text PRIMARY KEY,
    name        text NOT NULL,
    description text,
    project_id  text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS items_name_unique
    ON items (lower(btrim(name)));

CREATE INDEX IF NOT EXISTS items_seq_idx ON items (seq);

-- Supabase exposes every public table through its REST API (PostgREST).
-- This app talks to Postgres directly, so lock the table away from the
-- anon/authenticated API roles: with RLS on and no policies, they see nothing.
ALTER TABLE items ENABLE ROW LEVEL SECURITY;
