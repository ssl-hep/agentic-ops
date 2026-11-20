-- The reporting projection (docs/temporal-production-plan.md: Bookkeeping).
--
-- Workflows own the state; these tables are what controller activities write about it, every
-- write an idempotent upsert (agentic_ops_worker/reporting.py). The invariants and what remains
-- are views, so the TUI and the backend read them the same way the activities do.
--
-- The value lists in CHECK constraints mirror Python enums (units.UnitState, signatures.Action,
-- failures.FailureClass, records.*); tests/test_migrations.py checks they agree. Change both in a
-- new migration.

CREATE TABLE production.selections (
    selection_id text PRIMARY KEY,
    analysis text NOT NULL,
    content_hash text NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    approved_by text,
    approved_at timestamptz,
    -- What the approver saw: bytes to read, files with no disk replica, estimated core-hours
    totals jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE production.selection_dids (
    selection_id text NOT NULL REFERENCES production.selections ON DELETE CASCADE,
    did text NOT NULL,
    ordinal integer NOT NULL,
    PRIMARY KEY (selection_id, did),
    UNIQUE (selection_id, ordinal)
);

CREATE TABLE production.productions (
    production_id text PRIMARY KEY CONSTRAINT productions_id_check CHECK (production_id ~ '^[a-z0-9]{1,40}$'),
    analysis text NOT NULL,
    config_name text NOT NULL,
    clone_path text NOT NULL,
    commit_sha text NOT NULL,
    -- Digest of uncommitted changes in the clone, if any
    diff_digest text,
    release text NOT NULL,
    tarball_path text NOT NULL,
    tarball_digest text NOT NULL,
    tag text NOT NULL,
    selection_id text NOT NULL REFERENCES production.selections,
    owner text NOT NULL,
    state text NOT NULL DEFAULT 'CREATED' CONSTRAINT productions_state_check CHECK (
        state IN ('CREATED', 'CHECKING_BUILD', 'SMOKE_TEST', 'AWAITING_LAUNCH', 'RUNNING', 'PAUSED',
                  'COMPLETE', 'CANCELLED', 'FAILED')
    ),
    launch_approved_by text,
    launch_approved_at timestamptz,
    -- The production workflow's write counter: an update applies only with a larger seq
    seq bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE production.datasets (
    production_id text NOT NULL REFERENCES production.productions,
    did text NOT NULL,
    state text NOT NULL CONSTRAINT datasets_state_check CHECK (
        state IN ('PLANNING', 'RUNNING', 'COMPLETE', 'CANCELLED')
    ),
    snapshot_at timestamptz,
    completed_at timestamptz,
    -- Recorded at completion: events processed and sum of weights over accepted attempts
    events_done bigint,
    sum_of_weights double precision,
    -- The dataset workflow's write counter
    seq bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (production_id, did)
);

-- Every input file, frozen when the dataset is planned
CREATE TABLE production.dataset_snapshots (
    production_id text NOT NULL,
    did text NOT NULL,
    guid text NOT NULL,
    lfn text NOT NULL,
    size bigint NOT NULL,
    checksum text NOT NULL,
    events bigint NOT NULL,
    snapshot_at timestamptz NOT NULL,
    -- When file_replicas rows of this file were last resolved; a refresh older than this is stale
    replicas_resolved_at timestamptz NOT NULL,
    PRIMARY KEY (production_id, did, guid),
    FOREIGN KEY (production_id, did) REFERENCES production.datasets
);

-- Disk replicas of each snapshot file, replaced by refresh_replicas
CREATE TABLE production.file_replicas (
    production_id text NOT NULL,
    did text NOT NULL,
    guid text NOT NULL,
    rse text NOT NULL,
    -- root:// PFN without the XCache prefix
    pfn text NOT NULL,
    -- Preference order, 0 first
    rank integer NOT NULL,
    resolved_at timestamptz NOT NULL,
    last_outcome text CONSTRAINT file_replicas_outcome_check CHECK (last_outcome IN ('ok', 'failed')),
    last_error text,
    last_read_at timestamptz,
    PRIMARY KEY (production_id, did, guid, rse),
    FOREIGN KEY (production_id, did, guid) REFERENCES production.dataset_snapshots ON DELETE CASCADE
);
CREATE INDEX file_replicas_pfn ON production.file_replicas (production_id, guid, pfn);

CREATE TABLE production.units (
    production_id text NOT NULL,
    unit_id text NOT NULL,
    did text NOT NULL,
    state text NOT NULL CONSTRAINT units_state_check CHECK (
        state IN ('PLANNED', 'QUEUED', 'RUNNING', 'VERIFYING', 'DONE', 'RETRY_WAIT', 'BLOCKED',
                  'NEEDS_DECISION', 'EXCLUDED', 'SUPERSEDED', 'CANCELLED')
    ),
    -- Attempts started; attempt n wrote <unit_id>.a<n>.root
    attempt integer NOT NULL DEFAULT 0,
    failures integer NOT NULL DEFAULT 0,
    memclass text NOT NULL CONSTRAINT units_memclass_check CHECK (memclass IN ('4g', '8g', '16g')),
    size bigint NOT NULL,
    events_expected bigint NOT NULL,
    est_seconds integer NOT NULL,
    -- The unit this one was split from
    parent_id text,
    -- The approved decision that excluded this unit's files (EXCLUDED units)
    decision_id text,
    -- The dataset workflow's write counter: an update applies only with a larger seq
    seq bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (production_id, unit_id),
    UNIQUE (production_id, unit_id, did),
    FOREIGN KEY (production_id, did) REFERENCES production.datasets,
    FOREIGN KEY (production_id, parent_id) REFERENCES production.units DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX units_dataset ON production.units (production_id, did, state);
CREATE INDEX units_parent ON production.units (production_id, parent_id) WHERE parent_id IS NOT NULL;

-- A unit's files never change: a split or an exclusion makes new units
CREATE TABLE production.unit_files (
    production_id text NOT NULL,
    unit_id text NOT NULL,
    did text NOT NULL,
    guid text NOT NULL,
    ordinal integer NOT NULL,
    PRIMARY KEY (production_id, unit_id, guid),
    UNIQUE (production_id, unit_id, ordinal),
    FOREIGN KEY (production_id, unit_id, did) REFERENCES production.units (production_id, unit_id, did),
    -- A unit can list only files of its dataset's snapshot
    FOREIGN KEY (production_id, did, guid) REFERENCES production.dataset_snapshots
);
CREATE INDEX unit_files_file ON production.unit_files (production_id, did, guid);

-- One row per attempt, never deleted. Each column is written once: the first non-null value
-- recorded for it stays
CREATE TABLE production.attempts (
    production_id text NOT NULL,
    unit_id text NOT NULL,
    attempt integer NOT NULL CONSTRAINT attempts_attempt_check CHECK (attempt > 0),
    memclass text CONSTRAINT attempts_memclass_check CHECK (memclass IN ('4g', '8g', '16g')),
    queued_at timestamptz,
    started_at timestamptz,
    finished_at timestamptz,
    condor_job text,
    host text,
    outcome text CONSTRAINT attempts_outcome_check CHECK (
        outcome IN ('succeeded', 'failed', 'timed_out', 'cancelled')
    ),
    error_type text,
    failure_class text CONSTRAINT attempts_failure_class_check CHECK (
        failure_class IN ('infrastructure', 'transient_input', 'resources', 'bad_input', 'code', 'rejected')
    ),
    message text,
    output_path text,
    manifest_path text,
    log_path text,
    manifest jsonb,
    verification text CONSTRAINT attempts_verification_check CHECK (
        verification IN ('accepted', 'rejected', 'duplicate')
    ),
    verify_reasons jsonb,
    events bigint,
    sum_of_weights double precision,
    cleaned_up_at timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (production_id, unit_id, attempt),
    FOREIGN KEY (production_id, unit_id) REFERENCES production.units
);
CREATE INDEX attempts_accepted ON production.attempts (production_id, unit_id) WHERE verification = 'accepted';

-- One row per decision, mirroring the Updates and signals in workflow history. Only the outcome
-- (did the fix work?) is filled in later, once
CREATE TABLE production.decisions (
    decision_id text PRIMARY KEY,
    production_id text NOT NULL REFERENCES production.productions,
    did text,
    unit_id text,
    -- Failure fingerprint, for group decisions
    fingerprint text,
    -- signatures.Proposal as proposed, and as applied when a person changed it
    proposal jsonb NOT NULL,
    applied jsonb,
    -- The action applied
    action text NOT NULL CONSTRAINT decisions_action_check CHECK (
        action IN ('retry', 'retry_with_memory', 'split', 'exclude_files', 'new_production', 'escalate')
    ),
    verdict text NOT NULL CONSTRAINT decisions_verdict_check CHECK (verdict IN ('approved', 'rejected')),
    -- "policy:<rule>", "user:<name>" or "group:<fingerprint>"
    decided_by text NOT NULL,
    decided_at timestamptz NOT NULL,
    outcome text,
    outcome_at timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX decisions_unit ON production.decisions (production_id, unit_id);

-- Breaches of the invariants (docs: Invariants), one row each; empty when they hold. Snapshot
-- drift needs Rucio and is checked elsewhere.
CREATE VIEW production.invariant_violations AS
WITH live_files AS (
    SELECT s.production_id, s.did, s.guid, count(u.unit_id) AS n
    FROM production.dataset_snapshots s
    LEFT JOIN production.unit_files f USING (production_id, did, guid)
    LEFT JOIN production.units u ON u.production_id = f.production_id AND u.unit_id = f.unit_id
        AND u.state <> 'SUPERSEDED'
    GROUP BY s.production_id, s.did, s.guid
),
parent_files AS (
    SELECT f.production_id, f.unit_id AS parent_id, f.guid
    FROM production.unit_files f
    JOIN production.units p USING (production_id, unit_id)
    WHERE p.state = 'SUPERSEDED'
),
child_files AS (
    SELECT c.production_id, c.parent_id, f.guid, count(*) AS n
    FROM production.units c
    JOIN production.unit_files f USING (production_id, unit_id)
    WHERE c.parent_id IS NOT NULL
    GROUP BY c.production_id, c.parent_id, f.guid
),
partition_breaks AS (
    SELECT coalesce(pf.production_id, cf.production_id) AS production_id,
           coalesce(pf.parent_id, cf.parent_id) AS parent_id,
           coalesce(pf.guid, cf.guid) AS guid,
           CASE WHEN pf.guid IS NULL THEN 'a child lists a file its parent does not'
                WHEN cf.guid IS NULL THEN 'no child lists the file'
                ELSE format('%s children list the file', cf.n) END AS detail
    FROM parent_files pf
    FULL JOIN child_files cf USING (production_id, parent_id, guid)
    WHERE pf.guid IS NULL OR cf.guid IS NULL OR cf.n <> 1
),
unit_events AS (
    SELECT f.production_id, f.unit_id, sum(s.events) AS events
    FROM production.unit_files f
    JOIN production.dataset_snapshots s USING (production_id, did, guid)
    GROUP BY f.production_id, f.unit_id
),
accepted AS (
    SELECT production_id, unit_id, count(*) AS n
    FROM production.attempts
    WHERE verification = 'accepted'
    GROUP BY production_id, unit_id
)
SELECT 'file_not_in_one_live_unit' AS kind, production_id, did, NULL::text AS unit_id, guid,
       format('in %s live units', n) AS detail
FROM live_files
WHERE n <> 1
UNION ALL
SELECT 'children_do_not_partition', b.production_id, p.did, b.parent_id, b.guid, b.detail
FROM partition_breaks b
JOIN production.units p ON p.production_id = b.production_id AND p.unit_id = b.parent_id
WHERE p.state = 'SUPERSEDED'
UNION ALL
SELECT 'unit_events_mismatch', u.production_id, u.did, u.unit_id, NULL,
       format('events_expected %s, its files hold %s', u.events_expected, coalesce(e.events, 0))
FROM production.units u
LEFT JOIN unit_events e USING (production_id, unit_id)
WHERE u.events_expected <> coalesce(e.events, 0)
UNION ALL
SELECT 'excluded_without_approval', u.production_id, u.did, u.unit_id, NULL,
       format('decision %s', coalesce(u.decision_id, 'none'))
FROM production.units u
LEFT JOIN production.decisions d ON d.decision_id = u.decision_id AND d.production_id = u.production_id
    AND d.verdict = 'approved' AND d.action = 'exclude_files'
WHERE u.state = 'EXCLUDED' AND d.decision_id IS NULL
UNION ALL
SELECT 'complete_with_unfinished_units', u.production_id, u.did, u.unit_id, NULL,
       format('dataset is COMPLETE, unit is %s', u.state)
FROM production.datasets d
JOIN production.units u USING (production_id, did)
WHERE d.state = 'COMPLETE' AND u.state NOT IN ('DONE', 'EXCLUDED', 'SUPERSEDED')
UNION ALL
SELECT 'multiple_accepted_attempts', u.production_id, u.did, u.unit_id, NULL,
       format('%s accepted attempts', a.n)
FROM accepted a
JOIN production.units u USING (production_id, unit_id)
WHERE a.n > 1
UNION ALL
SELECT 'done_without_accepted_attempt', u.production_id, u.did, u.unit_id, NULL, 'no accepted attempt'
FROM production.units u
LEFT JOIN accepted a USING (production_id, unit_id)
WHERE u.state = 'DONE' AND a.n IS NULL;

-- Live units not finished, by dataset and state (docs: What remains)
CREATE VIEW production.what_remains AS
SELECT u.production_id, u.did, u.state,
       count(*) AS units,
       sum((SELECT count(*) FROM production.unit_files f
            WHERE f.production_id = u.production_id AND f.unit_id = u.unit_id))::bigint AS files,
       sum(u.events_expected)::bigint AS events,
       sum(u.est_seconds)::bigint AS est_seconds
FROM production.units u
WHERE u.state NOT IN ('DONE', 'EXCLUDED', 'SUPERSEDED', 'CANCELLED')
GROUP BY u.production_id, u.did, u.state;

-- Files of unfinished live units with no replica that has not failed: only a person can resolve
-- them (exclude, or stage outside agentic-ops and refresh)
CREATE VIEW production.files_without_replica AS
SELECT f.production_id, f.did, f.unit_id, f.guid
FROM production.unit_files f
JOIN production.units u USING (production_id, unit_id)
WHERE u.state NOT IN ('DONE', 'EXCLUDED', 'SUPERSEDED', 'CANCELLED')
  AND NOT EXISTS (
      SELECT 1 FROM production.file_replicas r
      WHERE r.production_id = f.production_id AND r.did = f.did AND r.guid = f.guid
        AND r.last_outcome IS DISTINCT FROM 'failed'
  );

-- Median wall time of accepted attempts, to turn units left into core-hours
CREATE VIEW production.unit_runtimes AS
SELECT production_id,
       count(*) AS accepted,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - started_at)::double precision) AS median_seconds
FROM production.attempts
WHERE verification = 'accepted' AND started_at IS NOT NULL AND finished_at IS NOT NULL
GROUP BY production_id;
