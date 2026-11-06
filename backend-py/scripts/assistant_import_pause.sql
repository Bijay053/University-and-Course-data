-- Runtime-compatible pause for legacy assistant auto-import proposals.
-- Provider opt-in is reversible through universities.scrape_config.
CREATE OR REPLACE FUNCTION pause_provider_assistant_imports() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE paused boolean; m jsonb; a jsonb; msgs jsonb='[]'; acts jsonb; request jsonb;
BEGIN
  SELECT coalesce((u.scrape_config->>'assistant_imports_paused')='true',false)
    INTO paused FROM scrape_runtime_jobs j JOIN universities u ON u.id=j.university_id
    WHERE j.runtime_job_id=NEW.job_id;
  IF NOT coalesce(paused,false) THEN RETURN NEW; END IF;
  SELECT value INTO request FROM jsonb_array_elements(NEW.messages) WITH ORDINALITY e(value,n)
    WHERE value->>'role'='user' ORDER BY n DESC LIMIT 1;
  IF request->>'_engine'='v2' OR request ? '_agentV2' THEN RETURN NEW; END IF;
  FOR m IN SELECT value FROM jsonb_array_elements(NEW.messages) LOOP
    IF m->>'role'='assistant' AND jsonb_typeof(m->'actions')='array' THEN
      acts='[]';
      FOR a IN SELECT value FROM jsonb_array_elements(m->'actions') LOOP
        IF a->>'state'='proposed' AND a->>'kind' IN
          ('scan_catalogue','recover_catalogue','review_courses','repair_catalogue') THEN
          a=a || '{"requiresConfirmation":true,"pauseReason":"Automatic imports paused until provider consolidation is reviewed."}'::jsonb;
        END IF;
        acts=acts || jsonb_build_array(a);
      END LOOP;
      m=jsonb_set(m,'{actions}',acts);
    END IF;
    msgs=msgs || jsonb_build_array(m);
  END LOOP;
  NEW.messages=msgs;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS provider_assistant_import_pause ON scrape_assistant_threads;
CREATE TRIGGER provider_assistant_import_pause BEFORE INSERT OR UPDATE OF messages
ON scrape_assistant_threads FOR EACH ROW EXECUTE FUNCTION pause_provider_assistant_imports();
