-- scripts/gdpr_delete.sql
-- Полное удаление ПДн пользователя по 152-ФЗ (ручной режим).
-- Запуск: psql -v platform="'tg'" -v user_id="123" -f scripts/gdpr_delete.sql
--
-- ТРЕБУЕТ миграции migrations/M0_security_migration.sql
-- (нужны таблицы gdpr_audit_log и gdpr_erasure_log).
--
-- ВНИМАНИЕ: этот скрипт дублирует путь удаления из core/gdpr.delete_user_data.
-- Чтобы списки таблиц не расходились, предпочтительно использовать Python:
--   python -c "from core.gdpr import delete_user_data; \
--              delete_user_data('tg', 123, admin_id=0, admin_platform='cli')"
BEGIN;
CREATE TEMP TABLE _app_ids ON COMMIT DROP AS
SELECT id FROM bright_city_applications WHERE platform = :platform AND user_id = :user_id;
DELETE FROM application_status_history WHERE application_id IN (SELECT id FROM _app_ids);
DELETE FROM bright_city_applications WHERE platform = :platform AND user_id = :user_id;
DELETE FROM bot_messages WHERE platform = :platform AND user_id = :user_id;
DELETE FROM bot_user_states WHERE platform = :platform AND user_id = :user_id;
DELETE FROM banned_users WHERE platform = :platform AND user_id = :user_id;
DELETE FROM admin_users WHERE platform = :platform AND admin_id = :user_id;
DELETE FROM admin_settings WHERE platform = :platform AND admin_id = :user_id;
DELETE FROM admin_sudo_sessions WHERE platform = :platform AND admin_id = :user_id;

-- ИСПРАВЛЕНО: раньше здесь стоял DELETE FROM media_files. Он уничтожал
-- очередь удаления, и фоновый воркер retry_pending_deletions больше не
-- видел эти записи — фото и квитанции детей НАВСЕГДА оставались на VK CDN,
-- при том что операция выглядела выполненной.
UPDATE media_files SET deleted_at = NOW()
 WHERE user_platform = :platform AND user_id = :user_id AND deleted_at IS NULL;

DELETE FROM gdpr_requests WHERE platform = :platform AND user_id = :user_id;

-- ИСПРАВЛЕНО: переписка пользователя и URL вложений в очереди не чистились.
DELETE FROM outbox_messages WHERE platform = :platform AND target_user_id = :user_id;

-- ИСПРАВЛЕНО: ручной путь не оставлял следа в журнале уничтожения ПДн.
INSERT INTO gdpr_audit_log (admin_id, admin_platform, target_platform,
                            target_user_id, action, details)
VALUES (0, 'cli', :platform, :user_id, 'delete_sql',
        '{"source":"gdpr_delete.sql"}'::jsonb);

-- ИСПРАВЛЕНО: запрет «воскрешения» ПДн из in-memory кэша процессов
-- (иначе следующее сообщение пользователя возвращало phone/name_age/
-- child_photo_urls обратно в bot_user_states).
INSERT INTO gdpr_erasure_log (platform, user_id, erased_at, admin_id)
VALUES (:platform, :user_id, NOW(), 0)
ON CONFLICT (platform, user_id) DO UPDATE SET erased_at = NOW();

COMMIT;

-- Строки media_files удаляются ТОЛЬКО после подтверждённого удаления файла
-- с CDN (иначе файл останется, а очередь пропадёт):
--   DELETE FROM media_files
--    WHERE user_platform = :platform AND user_id = :user_id
--      AND vk_delete_confirmed = TRUE;
