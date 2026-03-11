-- Защита от мута за баллы
CREATE TABLE IF NOT EXISTS mute_protection (
    user_id         BIGINT      NOT NULL,
    chat_id         BIGINT      NOT NULL,
    protected_until TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, chat_id)
);

CREATE INDEX IF NOT EXISTS idx_mute_protection_until
    ON mute_protection (protected_until);