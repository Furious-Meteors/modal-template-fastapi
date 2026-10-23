-- Shared rate limiting (RATE_LIMIT_BACKEND=postgres): one counter per (bucket, window).
-- Applied once by the migration runner. Old windows are pruned by PostgresRateLimiter.
CREATE TABLE IF NOT EXISTS rate_limits (
    bucket       text   NOT NULL,
    window_start bigint NOT NULL,   -- unix seconds, aligned to the window size
    hits         int    NOT NULL,
    PRIMARY KEY (bucket, window_start)
);
CREATE INDEX IF NOT EXISTS rate_limits_window_idx ON rate_limits (window_start);

-- Keep it out of Supabase's REST API (same reasoning as the other tables).
ALTER TABLE rate_limits ENABLE ROW LEVEL SECURITY;
