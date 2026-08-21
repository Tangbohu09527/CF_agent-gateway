-- CF_agent-gateway V1 Beta hardening migration for SQLite.
-- Preconditions: stop every HTTP/Worker writer and take a verified backup.
-- Nonzero legacy checkpoints require an explicit anchor or replay decision.

DROP TABLE IF EXISTS temp.cf_gateway_checkpoint_migration_guard;
CREATE TEMP TABLE cf_gateway_checkpoint_migration_guard (
    nonzero_checkpoint_count INTEGER NOT NULL
        CHECK (nonzero_checkpoint_count = 0)
);
INSERT INTO cf_gateway_checkpoint_migration_guard (nonzero_checkpoint_count)
SELECT COUNT(*)
FROM wechat_sync_checkpoints
WHERE last_local_id > 0;
DROP TABLE cf_gateway_checkpoint_migration_guard;

PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

ALTER TABLE wechat_sync_checkpoints RENAME TO wechat_sync_checkpoints_v1_backup;

CREATE TABLE wechat_sync_checkpoints (
    id INTEGER NOT NULL,
    source_account_id VARCHAR(255) NOT NULL,
    conversation_id VARCHAR(255) NOT NULL,
    last_local_id BIGINT NOT NULL,
    regression_generation BIGINT DEFAULT '0' NOT NULL,
    last_message_fingerprint VARCHAR(64),
    initialized_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_wechat_sync_checkpoint_account_conversation
        UNIQUE (source_account_id, conversation_id),
    CONSTRAINT ck_wechat_sync_checkpoint_nonnegative_local_id
        CHECK (last_local_id >= 0),
    CONSTRAINT ck_wechat_sync_checkpoint_nonnegative_generation
        CHECK (regression_generation >= 0),
    CONSTRAINT ck_wechat_sync_checkpoint_fingerprint_length
        CHECK (
            last_message_fingerprint IS NULL
            OR length(last_message_fingerprint) = 64
        )
);

INSERT INTO wechat_sync_checkpoints (
    id,
    source_account_id,
    conversation_id,
    last_local_id,
    regression_generation,
    last_message_fingerprint,
    initialized_at,
    updated_at
)
SELECT
    id,
    source_account_id,
    conversation_id,
    last_local_id,
    0,
    NULL,
    initialized_at,
    updated_at
FROM wechat_sync_checkpoints_v1_backup;

DROP TABLE wechat_sync_checkpoints_v1_backup;

CREATE TABLE IF NOT EXISTS hermes_dispatch_records (
    id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    workspace_id VARCHAR(36) NOT NULL,
    ai_thread_id VARCHAR(36) NOT NULL,
    status VARCHAR(11) NOT NULL,
    attempt_count INTEGER DEFAULT '1' NOT NULL,
    lease_token VARCHAR(36),
    lease_expires_at DATETIME,
    requested_hermes_thread_id VARCHAR(255) NOT NULL,
    result_hermes_thread_id VARCHAR(255),
    assistant_content TEXT,
    last_error_code VARCHAR(128),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_hermes_dispatch_message UNIQUE (message_id),
    CONSTRAINT ck_hermes_dispatch_attempt_count CHECK (attempt_count >= 1),
    CONSTRAINT ck_hermes_dispatch_state CHECK (
        (status = 'succeeded' AND lease_token IS NULL AND lease_expires_at IS NULL)
        OR (
            status = 'in_progress'
            AND lease_token IS NOT NULL
            AND lease_expires_at IS NOT NULL
        )
        OR (status = 'failed' AND lease_token IS NULL AND lease_expires_at IS NULL)
    ),
    CONSTRAINT ck_hermes_dispatch_succeeded_content
        CHECK (status != 'succeeded' OR assistant_content IS NOT NULL),
    FOREIGN KEY(message_id) REFERENCES messages (id) ON DELETE CASCADE,
    FOREIGN KEY(workspace_id) REFERENCES employee_workspaces (id) ON DELETE RESTRICT,
    FOREIGN KEY(ai_thread_id) REFERENCES ai_threads (id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_hermes_dispatch_records_status
    ON hermes_dispatch_records (status);

CREATE TABLE IF NOT EXISTS hermes_delivery_records (
    id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    ai_thread_id VARCHAR(36) NOT NULL,
    conversation_id VARCHAR(255) NOT NULL,
    content_sha256 VARCHAR(64) NOT NULL,
    status VARCHAR(11) NOT NULL,
    attempt_count INTEGER DEFAULT '1' NOT NULL,
    lease_token VARCHAR(36),
    lease_expires_at DATETIME,
    last_error_code VARCHAR(128),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_hermes_delivery_message UNIQUE (message_id),
    CONSTRAINT ck_hermes_delivery_attempt_count CHECK (attempt_count >= 1),
    CONSTRAINT ck_hermes_delivery_state CHECK (
        (status = 'succeeded' AND lease_token IS NULL AND lease_expires_at IS NULL)
        OR (
            status = 'in_progress'
            AND lease_token IS NOT NULL
            AND lease_expires_at IS NOT NULL
        )
        OR (status = 'failed' AND lease_token IS NULL AND lease_expires_at IS NULL)
    ),
    FOREIGN KEY(message_id) REFERENCES messages (id) ON DELETE CASCADE,
    FOREIGN KEY(ai_thread_id) REFERENCES ai_threads (id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_hermes_delivery_records_status
    ON hermes_delivery_records (status);

CREATE TABLE IF NOT EXISTS runtime_worker_status (
    worker_name VARCHAR(64) NOT NULL,
    instance_id VARCHAR(64) NOT NULL,
    process_id INTEGER NOT NULL,
    state VARCHAR(16) NOT NULL,
    hermes_enabled BOOLEAN NOT NULL,
    delivery_enabled BOOLEAN NOT NULL,
    started_at DATETIME NOT NULL,
    heartbeat_at DATETIME NOT NULL,
    last_cycle_started_at DATETIME,
    last_cycle_completed_at DATETIME,
    last_success_at DATETIME,
    last_error_code VARCHAR(128),
    source_logged_in BOOLEAN,
    chats_failed INTEGER NOT NULL,
    messages_seen INTEGER NOT NULL,
    messages_processed INTEGER NOT NULL,
    PRIMARY KEY (worker_name),
    CONSTRAINT ck_runtime_worker_status_state
        CHECK (state IN ('starting', 'idle', 'polling', 'degraded', 'stopped'))
);

COMMIT;
PRAGMA foreign_keys = ON;
PRAGMA foreign_key_check;
