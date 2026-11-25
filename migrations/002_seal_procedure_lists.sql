-- Existing requests are already submitted. Only new submissions start with an open list.
ALTER TABLE requests ADD COLUMN procedures_sealed boolean NOT NULL DEFAULT true;
ALTER TABLE requests ALTER COLUMN procedures_sealed SET DEFAULT false;

CREATE FUNCTION protect_procedure_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    req requests;
    actor text := nullif(current_setting('app.actor_id', true), '');
    trace uuid := nullif(current_setting('app.trace_id', true), '')::uuid;
BEGIN
    SELECT * INTO req FROM requests WHERE id = NEW.request_id;
    IF NOT FOUND OR req.procedures_sealed OR req.status != 'queued'
       OR actor IS DISTINCT FROM req.office_id OR trace IS NULL
       OR NEW.decision IS DISTINCT FROM 'pending' OR NEW.reason IS NOT NULL
       OR NEW.decided_by IS NOT NULL OR NEW.decided_at IS NOT NULL THEN
        RAISE EXCEPTION 'Procedures can only be added during submission'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER procedure_insert_protection BEFORE INSERT ON procedures
    FOR EACH ROW EXECUTE FUNCTION protect_procedure_insert();

CREATE FUNCTION protect_procedure_seal() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    actor text := nullif(current_setting('app.actor_id', true), '');
    trace uuid := nullif(current_setting('app.trace_id', true), '')::uuid;
BEGIN
    IF NEW.procedures_sealed IS DISTINCT FROM OLD.procedures_sealed AND (
        OLD.procedures_sealed OR NOT NEW.procedures_sealed OR OLD.status != 'queued'
        OR actor IS DISTINCT FROM OLD.office_id OR trace IS NULL
        OR (to_jsonb(NEW) - 'procedures_sealed') IS DISTINCT FROM
           (to_jsonb(OLD) - 'procedures_sealed')
    ) THEN
        RAISE EXCEPTION 'A submitted procedure list cannot be reopened or changed'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER procedure_seal_protection BEFORE UPDATE OF procedures_sealed ON requests
    FOR EACH ROW EXECUTE FUNCTION protect_procedure_seal();

CREATE FUNCTION require_procedure_seal() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM requests WHERE id = NEW.id AND NOT procedures_sealed) THEN
        RAISE EXCEPTION 'Seal the procedure list before committing a submission'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER procedure_list_sealed AFTER INSERT OR UPDATE ON requests
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_procedure_seal();

-- Sealing completes the already-audited submission. All other updates retain the original audit.
-- The BEFORE trigger permits only the seal itself to change when this update audit is skipped.
DROP TRIGGER request_audit ON requests;
CREATE TRIGGER request_audit AFTER INSERT ON requests
    FOR EACH ROW EXECUTE FUNCTION audit_request_change();
CREATE TRIGGER request_update_audit AFTER UPDATE ON requests
    FOR EACH ROW WHEN (OLD.procedures_sealed = NEW.procedures_sealed)
    EXECUTE FUNCTION audit_request_change();
