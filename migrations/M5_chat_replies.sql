-- Даёт возможность отвечать на конкретное сообщение в чате.
-- Также гарантирует, что у bot_messages есть id (на случай, если его нет).

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'bot_messages' AND column_name = 'id'
    ) THEN
        ALTER TABLE bot_messages ADD COLUMN id BIGSERIAL PRIMARY KEY;
    END IF;
END $$;

ALTER TABLE bot_messages
    ADD COLUMN IF NOT EXISTS reply_to_id BIGINT,
    ADD COLUMN IF NOT EXISTS reply_to_text TEXT,
    ADD COLUMN IF NOT EXISTS reply_to_direction TEXT;

CREATE INDEX IF NOT EXISTS idx_bot_messages_reply_to_id
    ON bot_messages (reply_to_id)
    WHERE reply_to_id IS NOT NULL;