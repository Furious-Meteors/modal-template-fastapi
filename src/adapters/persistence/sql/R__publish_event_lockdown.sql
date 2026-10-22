-- REPEATABLE: runs on EVERY start, after the versioned migrations (see migrations.py).
--
-- Supabase exposes every function in the public schema through its REST API (PostgREST) and
-- grants EXECUTE to its API roles by default: anyone holding the project's anon key could
-- publish events. Keep it callable only by the role the app connects as (the owner).
REVOKE ALL ON FUNCTION publish_event(text, text, jsonb) FROM PUBLIC;
DO $$
DECLARE
    api_role text;
BEGIN
    FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
            EXECUTE format('REVOKE ALL ON FUNCTION publish_event(text, text, jsonb) FROM %I', api_role);
        END IF;
    END LOOP;
END
$$;
