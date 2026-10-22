-- Publish an event in ONE round trip.
--
-- The event bus must (1) take a database-wide lock so event ids are assigned in commit order,
-- (2) insert the row and (3) NOTIFY. Done statement by statement from the client that is a
-- 5-step transaction, and because the lock is held until commit it caps the WHOLE system's
-- writes at about 1 / (5 x round-trip time). As a function it is a single statement: one round
-- trip, and the lock is held for only the time it takes the database to run it.
--
-- Who may call it is set by R__publish_event_lockdown.sql (re-asserted on every start).
--
-- The lock key MUST equal _PUBLISH_LOCK_KEY in src/adapters/events/postgres.py
-- (a test compares them). Returns text/float columns so the client needs no jsonb codec.
CREATE OR REPLACE FUNCTION publish_event(p_topic text, p_type text, p_data jsonb)
RETURNS TABLE (out_seq bigint, out_topic text, out_type text, out_data text, out_ts double precision)
LANGUAGE plpgsql
SET search_path = public, pg_catalog
AS $$
DECLARE
    r events%ROWTYPE;
BEGIN
    PERFORM pg_advisory_xact_lock(7340002);
    INSERT INTO events (topic, type, data) VALUES (p_topic, p_type, p_data) RETURNING * INTO r;
    PERFORM pg_notify('events', r.seq::text);
    RETURN QUERY SELECT r.seq, r.topic, r.type, r.data::text, extract(epoch FROM r.ts)::float8;
END;
$$;
