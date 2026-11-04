-- Архив чатов: админ может явно убрать диалог из рабочего списка.
-- Если после архивации пользователь снова написал — сравнение timestamps
-- в SQL вернёт диалог в Актуальные автоматически.
CREATE TABLE IF NOT EXISTS chat_archives (
    platform     TEXT        NOT NULL,
    user_id      BIGINT      NOT NULL,
    archived_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_by  INT,
    PRIMARY KEY (platform, user_id)
);

-- Ускоряем выборку «последнее сообщение по (platform, user_id)».
CREATE INDEX IF NOT EXISTS idx_bot_messages_platform_user_created
    ON bot_messages (platform, user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_apps_platform_user_created
    ON bright_city_applications (platform, user_id, created_at DESC);