CREATE TABLE practices (
    id text PRIMARY KEY,
    timezone text NOT NULL
);
CREATE TABLE users (
    id text PRIMARY KEY,
    role text NOT NULL CHECK (role IN ('member', 'office', 'reviewer', 'admin')),
    practice_id text REFERENCES practices,
    principal_type text NOT NULL DEFAULT 'user' CHECK (principal_type IN ('user', 'service')),
    CHECK ((role = 'office') = (practice_id IS NOT NULL))
);
CREATE TABLE members (
    id text PRIMARY KEY,
    user_id text NOT NULL UNIQUE REFERENCES users
);
CREATE TABLE practice_members (
    practice_id text REFERENCES practices,
    member_id text REFERENCES members,
    PRIMARY KEY (practice_id, member_id)
);
CREATE TABLE coverages (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    member_id text NOT NULL REFERENCES members,
    starts_on date NOT NULL,
    ends_on date NOT NULL,
    CHECK (ends_on >= starts_on),
    UNIQUE (member_id, starts_on, ends_on)
);
CREATE TABLE reviewer_patients (
    reviewer_id text REFERENCES users,
    member_id text REFERENCES members,
    PRIMARY KEY (reviewer_id, member_id)
);
CREATE TABLE requests (
    id uuid PRIMARY KEY,
    member_id text NOT NULL REFERENCES members,
    office_id text NOT NULL REFERENCES users,
    diagnosis text NOT NULL,
    service_date date NOT NULL,
    clinical_note text,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'in_review', 'decided')),
    reviewer_id text REFERENCES users,
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz,
    CHECK ((status = 'queued') = (reviewer_id IS NULL)),
    CHECK ((status = 'decided') = (decided_at IS NOT NULL))
);
CREATE TABLE procedures (
    id uuid PRIMARY KEY,
    request_id uuid NOT NULL REFERENCES requests,
    code text NOT NULL,
    units integer NOT NULL CHECK (units BETWEEN 1 AND 30),
    decision text NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending', 'approved', 'denied')),
    reason text,
    decided_by text REFERENCES users,
    decided_at timestamptz,
    UNIQUE (request_id, code),
    CHECK (decision != 'denied' OR length(btrim(reason)) > 0 AND reason IS NOT NULL),
    CHECK ((decision = 'pending') = (decided_by IS NULL)),
    CHECK ((decision = 'pending') = (decided_at IS NULL))
);
CREATE TABLE audit_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id uuid NOT NULL REFERENCES requests,
    actor_id text NOT NULL REFERENCES users,
    action text NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    trace_id uuid NOT NULL,
    details jsonb NOT NULL
);
CREATE TABLE write_receipts (
    actor_id text NOT NULL REFERENCES users,
    idempotency_key uuid NOT NULL,
    operation text NOT NULL,
    fingerprint text NOT NULL,
    request_id uuid NOT NULL REFERENCES requests,
    PRIMARY KEY (actor_id, idempotency_key)
);
CREATE TABLE access_events (
    trace_id uuid PRIMARY KEY,
    actor_id text REFERENCES users,
    method text NOT NULL,
    route text NOT NULL,
    status integer NOT NULL,
    error_codes jsonb NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX requests_queue ON requests (created_at, id) WHERE status = 'queued';
CREATE INDEX requests_member ON requests (member_id, created_at);
CREATE INDEX requests_office ON requests (office_id, created_at);
CREATE INDEX requests_reviewer ON requests (reviewer_id, created_at);
CREATE INDEX audit_request ON audit_events (request_id, id);

-- Audit insertion is mandatory and part of the same transaction as each transition.
CREATE FUNCTION audit_request_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    event_action text;
    actor text := nullif(current_setting('app.actor_id', true), '');
    trace uuid := nullif(current_setting('app.trace_id', true), '')::uuid;
BEGIN
    IF TG_OP = 'INSERT' THEN
        event_action := 'submitted';
    ELSE
        IF OLD.status = 'decided' THEN
            RAISE EXCEPTION 'Decisions are final' USING ERRCODE = '23514';
        END IF;
        IF ROW(NEW.id, NEW.member_id, NEW.office_id, NEW.diagnosis, NEW.service_date,
               NEW.clinical_note, NEW.created_at)
           IS DISTINCT FROM
           ROW(OLD.id, OLD.member_id, OLD.office_id, OLD.diagnosis, OLD.service_date,
               OLD.clinical_note, OLD.created_at) THEN
            RAISE EXCEPTION 'Submitted content is immutable' USING ERRCODE = '23514';
        END IF;
        IF NEW.status = 'decided' AND OLD.status = 'in_review' THEN
            event_action := 'decided';
        ELSIF NEW.status = 'in_review' AND OLD.status = 'queued' THEN
            event_action := 'claimed';
        ELSIF NEW.status = 'in_review' AND OLD.status = 'in_review'
              AND NEW.reviewer_id IS DISTINCT FROM OLD.reviewer_id THEN
            event_action := 'reassigned';
        ELSE
            RAISE EXCEPTION 'Invalid request transition' USING ERRCODE = '23514';
        END IF;
    END IF;
    INSERT INTO audit_events (request_id, actor_id, action, trace_id, details)
    VALUES (NEW.id, actor, event_action, trace,
        jsonb_build_object('status', NEW.status, 'reviewer_id', NEW.reviewer_id)
        || CASE WHEN TG_OP = 'UPDATE' THEN
            jsonb_build_object('previous_reviewer_id', OLD.reviewer_id)
           ELSE '{}'::jsonb END);
    RETURN NEW;
END $$;
CREATE TRIGGER request_audit AFTER INSERT OR UPDATE ON requests
    FOR EACH ROW EXECUTE FUNCTION audit_request_change();

CREATE FUNCTION protect_procedure() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.decision != 'pending' OR
       ROW(NEW.id, NEW.request_id, NEW.code, NEW.units) IS DISTINCT FROM
       ROW(OLD.id, OLD.request_id, OLD.code, OLD.units) THEN
        RAISE EXCEPTION 'Procedure content and final decisions are immutable'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.decided_by IS DISTINCT FROM nullif(current_setting('app.actor_id', true), '') THEN
        RAISE EXCEPTION 'Decision must identify its actor' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER procedure_protection BEFORE UPDATE ON procedures
    FOR EACH ROW EXECUTE FUNCTION protect_procedure();

-- Deferred checks see the entire decision, never a partly updated request.
CREATE FUNCTION check_request_complete() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    req requests;
    target uuid;
BEGIN
    IF TG_TABLE_NAME = 'requests' THEN target := NEW.id;
    ELSE target := NEW.request_id;
    END IF;
    SELECT * INTO req FROM requests WHERE id = target;
    IF NOT EXISTS (SELECT 1 FROM procedures WHERE request_id = target) THEN
        RAISE EXCEPTION 'A request needs at least one procedure' USING ERRCODE = '23514';
    END IF;
    IF req.status = 'decided' THEN
        IF EXISTS (SELECT 1 FROM procedures WHERE request_id = target AND
            (decision = 'pending' OR decided_by IS DISTINCT FROM req.reviewer_id)) THEN
            RAISE EXCEPTION 'All procedures need the assigned reviewer decision'
                USING ERRCODE = '23514';
        END IF;
    ELSIF EXISTS (SELECT 1 FROM procedures WHERE request_id = target AND decision != 'pending') THEN
        RAISE EXCEPTION 'Procedure decisions must be submitted together' USING ERRCODE = '23514';
    END IF;
    IF req.reviewer_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM reviewer_patients
        WHERE reviewer_id = req.reviewer_id AND member_id = req.member_id
    ) THEN
        RAISE EXCEPTION 'Reviewer treats this patient' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER request_complete AFTER INSERT OR UPDATE ON requests
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_request_complete();
CREATE CONSTRAINT TRIGGER procedure_complete AFTER INSERT OR UPDATE ON procedures
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_request_complete();

CREATE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'This record is append-only' USING ERRCODE = '23514';
END $$;
CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_change();
CREATE TRIGGER access_immutable BEFORE UPDATE OR DELETE ON access_events
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER access_no_truncate BEFORE TRUNCATE ON access_events
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_change();
CREATE TRIGGER request_no_delete BEFORE DELETE ON requests
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER procedure_no_delete BEFORE DELETE ON procedures
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
