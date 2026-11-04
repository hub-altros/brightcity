-- platform_message_id — id сообщения в TG/VK/MAX (для нативного reply/edit)
-- edited_at — когда сообщение правили в админке
ALTER TABLE bot_messages
    ADD COLUMN IF NOT EXISTS platform_message_id TEXT,
    ADD COLUMN IF NOT EXISTS edited_at TIMESTAMPTZ;
	ALTER TABLE outbox_messages
    ADD COLUMN IF NOT EXISTS bot_message_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_bot_messages_platform_msg_id
    ON bot_messages (platform_message_id)
    WHERE platform_message_id IS NOT NULL;