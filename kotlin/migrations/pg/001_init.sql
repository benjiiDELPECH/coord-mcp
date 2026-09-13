-- coord-mcp — schéma PostgreSQL.
--
-- Différences ASSUMÉES avec la version SQLite : ce ne sont pas des traductions
-- mécaniques, ce sont des corrections que PostgreSQL rend possibles.
--
--   created_at / updated_at : TIMESTAMPTZ au lieu de TEXT.
--       En SQLite, trois formats coexistaient réellement (ISO+offset, ISO Z, et
--       « 2026-08-27 23:21:57 » sans fuseau), dont un sur la ligne wi_83ED1B31.
--       En PG, la colonne ne PEUT PAS contenir autre chose qu'un instant.
--
--   triggers_ci : BOOLEAN au lieu de INTEGER 0/1.
--
--   status : CHECK sur les valeurs réellement observées.
--
--   revision : clé de concurrence optimiste (migration 001 côté SQLite).

CREATE TABLE IF NOT EXISTS work_items (
    id                     TEXT PRIMARY KEY,
    repo                   TEXT        NOT NULL,
    title                  TEXT        NOT NULL,
    scope_files            TEXT,
    scope_adr_topic        TEXT,
    github_issue_number    INTEGER,
    milestone_number       INTEGER,
    branch_name            TEXT,
    agent_id               TEXT,
    status                 TEXT        NOT NULL
        CHECK (status IN ('declared', 'claimed', 'in_progress', 'checked_out', 'released', 'abandoned')),
    eta_hours              DOUBLE PRECISION,
    manifest_path          TEXT,
    outcome                TEXT,
    created_at             TIMESTAMPTZ NOT NULL,
    updated_at             TIMESTAMPTZ NOT NULL,
    scope_symbols          TEXT,
    scope_symbols_expanded TEXT,
    triggers_ci            BOOLEAN     NOT NULL DEFAULT FALSE,
    revision               INTEGER     NOT NULL DEFAULT 1 CHECK (revision >= 1),

    -- Invariant MESURÉ sur les 2005 items terminaux réels : tous portent un
    -- outcome non vide. Ici il est porté par la BASE, pas seulement par le code —
    -- aucun écrivain, même un script d'administration, ne peut l'enfreindre.
    CONSTRAINT terminal_implique_outcome
        CHECK (status NOT IN ('released', 'abandoned') OR (outcome IS NOT NULL AND btrim(outcome) <> '')),
    CONSTRAINT updated_apres_created
        CHECK (updated_at >= created_at)
);

CREATE INDEX IF NOT EXISTS idx_work_items_status ON work_items (status);
CREATE INDEX IF NOT EXISTS idx_work_items_repo ON work_items (repo);
CREATE INDEX IF NOT EXISTS idx_work_items_active ON work_items (status) WHERE status IN ('declared', 'claimed', 'in_progress', 'checked_out');

-- Outbox : événement à publier, écrit dans la MÊME transaction que la mutation
-- de work_items. C'est ce qui supprime le dual-write (UPDATE puis appel externe),
-- où l'un peut réussir pendant que l'autre échoue.
CREATE TABLE IF NOT EXISTS outbox_events (
    event_id          TEXT PRIMARY KEY,
    work_item_id      TEXT        NOT NULL REFERENCES work_items (id) ON DELETE CASCADE,
    event_type        TEXT        NOT NULL,
    payload           TEXT        NOT NULL,
    status            TEXT        NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'CLAIMED', 'SYNCED', 'FAILED', 'PARKED')),
    destination       TEXT,
    external_id       TEXT,
    synced_at         TIMESTAMPTZ,
    attempts          INTEGER     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error        TEXT,
    next_attempt_at   TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON outbox_events (next_attempt_at)
    WHERE status IN ('PENDING', 'FAILED');

-- Inbox : protège chaque CONSOMMATEUR des doublons. Un relay peut republier un
-- événement s'il meurt après publication avant d'avoir marqué l'outbox.
-- At-least-once + consommateurs idempotents = effectivement une seule fois.
CREATE TABLE IF NOT EXISTS consumer_inbox (
    consumer_name  TEXT        NOT NULL,
    event_id       TEXT        NOT NULL,
    processed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    result_digest  TEXT,
    PRIMARY KEY (consumer_name, event_id)
);
