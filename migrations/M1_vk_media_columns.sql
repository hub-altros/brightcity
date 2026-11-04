-- migrations/M1_vk_media_columns.sql
-- Долгосрочное хранение VK-ссылок для relay-нутых медиа.
--
-- Проблема: после relay MAX/TG → VK в media_files оставался только
-- оригинальный url (MAX CDN / TG file_id). MAX CDN живёт 1–24 часа,
-- TG file_id бесполезен для VK-админа. Через сутки админ открывал
-- заявку — и не мог получить ни одного вложения.
--
-- Решение: адаптер при успешном relay пишет в media_files
-- vk_url (HTTP VK CDN), vk_owner_id, vk_photo_id (или vk_doc_id).
-- _send_media_items_async использует эти поля приоритетно.
--
-- Идемпотентно.

BEGIN;

ALTER TABLE media_files ADD COLUMN IF NOT EXISTS vk_url       TEXT;
ALTER TABLE media_files ADD COLUMN IF NOT EXISTS vk_owner_id  BIGINT;
ALTER TABLE media_files ADD COLUMN IF NOT EXISTS vk_photo_id  BIGINT;
ALTER TABLE media_files ADD COLUMN IF NOT EXISTS vk_doc_id    BIGINT;

INSERT INTO schema_migrations (version) VALUES ('M1_vk_media_columns')
    ON CONFLICT (version) DO NOTHING;

COMMIT;