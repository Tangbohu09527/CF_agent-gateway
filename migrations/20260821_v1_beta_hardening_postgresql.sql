-- CF_agent-gateway V1 Beta hardening migration for PostgreSQL.
-- Preconditions: stop every HTTP/Worker writer and take a verified backup.
-- Nonzero legacy checkpoints require an explicit anchor or replay decision.

BEGIN;

LOCK TABLE wechat_sync_checkpoints IN ACCESS EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM wechat_sync_checkpoints
        WHERE last_local_id > 0
    ) THEN
        RAISE EXCEPTION
            'nonzero legacy checkpoints require explicit anchor backfill or approved replay';
    END IF;
END;
$$;

ALTER TABLE wechat_sync_checkpoints
    ADD COLUMN regression_generation BIGINT,
    ADD COLUMN last_message_fingerprint VARCHAR(64);

UPDATE wechat_sync_checkpoints
SET
    regression_generation = 0,
    last_message_fingerprint = NULL;

ALTER TABLE wechat_sync_checkpoints
    ALTER COLUMN regression_generation SET DEFAULT 0,
    ALTER COLUMN regression_generation SET NOT NULL,
    ADD CONSTRAINT ck_wechat_sync_checkpoint_nonnegative_generation
        CHECK (regression_generation >= 0),
    ADD CONSTRAINT ck_wechat_sync_checkpoint_fingerprint_length
        CHECK (
            last_message_fingerprint IS NULL
            OR length(last_message_fingerprint) = 64
        );

CREATE TABLE IF NOT EXISTS hermes_dispatch_records (
    id SERIAL NOT NULL,
    message_id INTEGER NOT NULL,
    workspace_id VARCHAR(36) NOT NULL,
    ai_thread_id VARCHAR(36) NOT NULL,
    status VARCHAR(11) NOT NULL,
    attempt_count INTEGER DEFAULT '1' NOT NULL,
    lease_token VARCHAR(36),
    lease_expires_at TIMESTAMP WITH TIME ZONE,
    requested_hermes_thread_id VARCHAR(255) NOT NULL,
    result_hermes_thread_id VARCHAR(255),
    assistant_content TEXT,
    last_error_code VARCHAR(128),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
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
    id SERIAL NOT NULL,
    message_id INTEGER NOT NULL,
    ai_thread_id VARCHAR(36) NOT NULL,
    conversation_id VARCHAR(255) NOT NULL,
    content_sha256 VARCHAR(64) NOT NULL,
    status VARCHAR(11) NOT NULL,
    attempt_count INTEGER DEFAULT '1' NOT NULL,
    lease_token VARCHAR(36),
    lease_expires_at TIMESTAMP WITH TIME ZONE,
    last_error_code VARCHAR(128),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
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
    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    heartbeat_at TIMESTAMP WITH TIME ZONE NOT NULL,
    last_cycle_started_at TIMESTAMP WITH TIME ZONE,
    last_cycle_completed_at TIMESTAMP WITH TIME ZONE,
    last_success_at TIMESTAMP WITH TIME ZONE,
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
