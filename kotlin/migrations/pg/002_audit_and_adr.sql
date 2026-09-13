-- coord-mcp — tables manquantes de la première migration.
--
-- La migration 001 n'avait porté que `work_items`, `outbox_events` et
-- `consumer_inbox`. Or la base SQLite en compte SEPT. Les oublier n'aurait pas
-- provoqué une erreur au démarrage : cela aurait provoqué une PERTE SILENCIEUSE
-- à la bascule.
--
-- Le cas le plus grave est `adr_allocations` : 147 lignes qui enregistrent les
-- numéros d'ADR déjà alloués. Les perdre, c'est réautoriser des numéros déjà
-- pris — exactement la collision que coord-mcp existe pour empêcher. Une table
-- oubliée ne crie pas ; elle fait juste disparaître la garantie.
--
-- Types PostgreSQL : les horodatages étaient du TEXTE en SQLite, ils deviennent
-- TIMESTAMPTZ. Les `UNIQUE` deviennent de vraies contraintes de table.

CREATE TABLE IF NOT EXISTS adr_allocations (
    id            BIGSERIAL PRIMARY KEY,
    repo_path     TEXT        NOT NULL,
    adr_number    INTEGER     NOT NULL CHECK (adr_number > 0),
    topic_slug    TEXT        NOT NULL,
    filename      TEXT        NOT NULL,
    work_item_id  TEXT,
    allocated_to  TEXT,
    allocated_at  TIMESTAMPTZ NOT NULL,
    -- L'unicité est ce qui rend l'allocation atomique : deux agents qui
    -- réclament le même numéro ne peuvent pas aboutir tous les deux.
    UNIQUE (repo_path, adr_number)
);

CREATE INDEX IF NOT EXISTS idx_adr_repo ON adr_allocations (repo_path);

CREATE TABLE IF NOT EXISTS migration_allocations (
    id               BIGSERIAL PRIMARY KEY,
    repo_path        TEXT        NOT NULL,
    version          INTEGER     NOT NULL,
    description_slug TEXT        NOT NULL,
    filename         TEXT        NOT NULL,
    work_item_id     TEXT,
    allocated_to     TEXT,
    allocated_at     TIMESTAMPTZ NOT NULL,
    UNIQUE (repo_path, version)
);

CREATE INDEX IF NOT EXISTS idx_migration_repo ON migration_allocations (repo_path);

CREATE TABLE IF NOT EXISTS audit_log (
    id           BIGSERIAL PRIMARY KEY,
    timestamp    TIMESTAMPTZ NOT NULL,
    tool         TEXT        NOT NULL,
    args_json    TEXT,
    result_json  TEXT,
    agent_id     TEXT,
    work_item_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log (timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_tool ON audit_log (tool);
