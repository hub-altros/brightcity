-- migrations/M0_security_migration.sql
-- Миграция безопасности для Bright City. Применять ПЕРВОЙ, до патчей кода.
--
-- ВАЖНО: перед запуском сделайте дамп БД:
--   pg_dump "$DB_NAME" -f pre_m0_$(date +%s).sql
--
-- Запуск:
--   psql "$DB_NAME" -v ON_ERROR_STOP=1 -f migrations/M0_security_migration.sql
--
-- НЕ добавляйте ключ -1/--single-transaction: в файле уже есть BEGIN/COMMIT,
-- и вы получите "WARNING: there is already a transaction in progress".
-- Если предыдущий запуск упал — просто запустите заново в НОВОЙ сессии psql
-- (транзакция откатывается целиком, поэтому повторный запуск безопасен).
--
-- ПРОВЕРКА ТЕКУЩЕГО СОСТОЯНИЯ (если сомневаетесь, применилось ли что-то):
--   SELECT table_name, column_name FROM information_schema.columns
--    WHERE table_schema = current_schema()
--      AND ((table_name = 'media_files'      AND column_name IN ('doc_id','full_relay_last_attempt_at'))
--        OR (table_name = 'outbox_messages'  AND column_name = 'status')
--        OR (table_name = 'gdpr_erasure_log' AND column_name = 'erased_at'));
--   Пусто → не применилось ничего, запускайте файл целиком.
--
-- Идемпотентно: можно запускать повторно.

BEGIN;

-- ============================================================
-- 1) Журнал стирания ПДн
--    Нужен, чтобы кэш UserState в процессах VK/TG не «воскрешал»
--    удалённые данные (phone, name_age, child_photo_urls) при
--    следующем сообщении пользователя.
-- ============================================================
CREATE TABLE IF NOT EXISTS gdpr_erasure_log (
    platform   TEXT NOT NULL,
    user_id    BIGINT NOT NULL,
    erased_at  TIMESTAMP NOT NULL DEFAULT NOW(),
    admin_id   BIGINT,
    PRIMARY KEY (platform, user_id)
);

-- ============================================================
-- 2) Документы (квитанции): идентификаторы для удаления с VK CDN
--    Без doc_id очередь удаления их не видит (owner_id/photo_id = NULL),
--    поэтому квитанции с ПДн оставались на CDN навсегда.
-- ============================================================
ALTER TABLE media_files ADD COLUMN IF NOT EXISTS doc_id BIGINT;
CREATE INDEX IF NOT EXISTS idx_media_files_pending_delete_doc
    ON media_files (deleted_at, vk_delete_confirmed, kind);

-- ============================================================
-- 3) Full relay: время последней попытки вместо времени запроса
-- ============================================================
ALTER TABLE media_files ADD COLUMN IF NOT EXISTS full_relay_last_attempt_at TIMESTAMP;

-- ============================================================
-- 4) Outbox: статусная модель вместо sent_at-до-отправки
-- ============================================================
ALTER TABLE outbox_messages
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_outbox_idem
    ON outbox_messages (idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON outbox_messages (platform, status, created_at)
    WHERE status IN ('pending', 'in_progress');

-- ============================================================
-- 5) Согласие на обработку ПДн + идемпотентность заявок
-- ============================================================
ALTER TABLE bright_city_applications
    ADD COLUMN IF NOT EXISTS consent_version   TEXT,
    ADD COLUMN IF NOT EXISTS consent_text_hash TEXT,
    ADD COLUMN IF NOT EXISTS consent_at        TIMESTAMP,
    ADD COLUMN IF NOT EXISTS consent_platform  TEXT,
    ADD COLUMN IF NOT EXISTS anonymized_at     TIMESTAMP,
    ADD COLUMN IF NOT EXISTS client_request_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_bca_client_request
    ON bright_city_applications (client_request_id)
    WHERE client_request_id IS NOT NULL;

-- Анонимизация больше не схлопывает всех в user_id = 0
ALTER TABLE bright_city_applications ALTER COLUMN user_id DROP NOT NULL;

-- ============================================================
-- 6) Нормализация платформы (регистр) — перед CHECK-ограничениями.
--    ПРОВЕРЬТЕ результат: значения вне ('vk','tg','max') надо
--    привести вручную ДО шага 7.
-- ============================================================
UPDATE bright_city_applications SET platform      = lower(platform)      WHERE platform <> lower(platform);
UPDATE bot_user_states         SET platform      = lower(platform)      WHERE platform <> lower(platform);
UPDATE bot_messages            SET platform      = lower(platform)      WHERE platform <> lower(platform);
UPDATE banned_users            SET platform      = lower(platform)      WHERE platform <> lower(platform);
UPDATE admin_users             SET platform      = lower(platform)      WHERE platform <> lower(platform);
UPDATE admin_settings          SET platform      = lower(platform)      WHERE platform <> lower(platform);
UPDATE gdpr_requests           SET platform      = lower(platform)      WHERE platform <> lower(platform);
UPDATE media_files             SET user_platform = lower(user_platform) WHERE user_platform <> lower(user_platform);

-- ============================================================
-- 7) CHECK-ограничения на platform (NOT VALID + VALIDATE)
--    Если VALIDATE предупреждает — остались недопустимые значения:
--      SELECT platform, count(*) FROM <таблица> GROUP BY platform;
-- ============================================================
DO $$
DECLARE
    t record;
BEGIN
    FOR t IN
        SELECT * FROM (VALUES
            ('bright_city_applications', 'platform',      'chk_bca_platform'),
            ('bot_user_states',          'platform',      'chk_bus_platform'),
            ('bot_messages',             'platform',      'chk_bm_platform'),
            ('banned_users',             'platform',      'chk_bu_platform'),
            ('admin_users',              'platform',      'chk_au_platform'),
            ('admin_settings',           'platform',      'chk_as_platform'),
            ('gdpr_requests',            'platform',      'chk_gr_platform'),
            ('media_files',              'user_platform', 'chk_mf_platform')
        ) AS v(tbl, col, cname)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = t.cname) THEN
            EXECUTE format(
                'ALTER TABLE %I ADD CONSTRAINT %I CHECK (%I IN (''vk'',''tg'',''max'')) NOT VALID',
                t.tbl, t.cname, t.col);
        END IF;
        BEGIN
            EXECUTE format('ALTER TABLE %I VALIDATE CONSTRAINT %I', t.tbl, t.cname);
        EXCEPTION WHEN check_violation THEN
            RAISE WARNING 'CONSTRAINT % не прошла VALIDATE: недопустимые platform в %', t.cname, t.tbl;
        END;
    END LOOP;
END $$;

-- ============================================================
-- 8) Медиа: владелец в ключе вместо глобального UNIQUE(url)
--
-- ИСПРАВЛЕНО ПО ФАКТУ ОШИБКИ 23505:
--   * uq_mf_source_file (UNIQUE по source_file_id) создать НЕЛЬЗЯ:
--     один и тот же TG file_id встречается в данных многократно
--     (файл пересылали повторно, он релаялся несколько раз). Заменён
--     обычным индексом — код на него не опирается.
--   * UNIQUE(source_file_id) требовался бы для защиты от повторной
--     заливки того же файла, но на существующих данных это невозможно
--     без потери строк. Если понадобится — вводить как отдельную
--     задачу с ручным разбором дублей.
-- ============================================================

-- На случай, если глобальный UNIQUE(url) уже был снят предыдущим запуском
-- и в таблице успели появиться дубли: схлопываем, сохраняя «более важную»
-- строку (сначала помеченную на удаление, затем с запрошенным relay).
WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY user_platform, user_id, url
               ORDER BY (deleted_at IS NOT NULL) DESC,
                        (full_relay_requested_at IS NOT NULL) DESC,
                        id ASC
           ) AS rn
      FROM media_files
)
DELETE FROM media_files WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

ALTER TABLE media_files DROP CONSTRAINT IF EXISTS media_files_url_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_mf_owner_url
    ON media_files (user_platform, user_id, url);

-- НЕ уникальный: см. комментарий выше.
CREATE INDEX IF NOT EXISTS idx_mf_source_file
    ON media_files (source_file_id) WHERE source_file_id IS NOT NULL;

-- ============================================================
-- 9) Ссылочная целостность.
--    ПЕРЕД применением вычистите орфаны, иначе ADD CONSTRAINT упадёт:
--      DELETE FROM application_status_history h
--       WHERE NOT EXISTS (SELECT 1 FROM bright_city_applications a WHERE a.id = h.application_id);
--      UPDATE bot_messages m SET application_id = NULL
--       WHERE application_id IS NOT NULL
--         AND NOT EXISTS (SELECT 1 FROM bright_city_applications a WHERE a.id = m.application_id);
-- ============================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_ash_app') THEN
        ALTER TABLE application_status_history
            ADD CONSTRAINT fk_ash_app FOREIGN KEY (application_id)
            REFERENCES bright_city_applications(id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_bm_app') THEN
        ALTER TABLE bot_messages
            ADD CONSTRAINT fk_bm_app FOREIGN KEY (application_id)
            REFERENCES bright_city_applications(id) ON DELETE SET NULL;
    END IF;
EXCEPTION WHEN foreign_key_violation THEN
    RAISE WARNING 'FK не созданы: сначала вычистите орфаны (см. комментарий выше)';
END $$;

-- ============================================================
-- 10) GDPR-запросы: один незакрытый на пользователя
--
-- ВАЖНО: старый код вставлял запросы БЕЗ дедупликации, поэтому у части
-- пользователей лежит несколько строк fulfilled = FALSE. Без чистки
-- CREATE UNIQUE INDEX упадёт с той же ошибкой 23505.
-- Оставляем самый ранний запрос, остальные незакрытые дубли удаляем:
-- это повторные нажатия одной и той же кнопки, отдельной ценности у них нет.
-- ============================================================
DELETE FROM gdpr_requests g
 USING gdpr_requests k
 WHERE g.platform = k.platform
   AND g.user_id  = k.user_id
   AND g.fulfilled = FALSE
   AND k.fulfilled = FALSE
   AND g.id > k.id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_gdpr_requests_pending
    ON gdpr_requests (platform, user_id) WHERE fulfilled = FALSE;

-- ============================================================
-- 11) GIN-индексы для JSONB-поиска
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_bca_child_photos_gin
    ON bright_city_applications USING gin (child_photo_urls jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_bca_drawings_gin
    ON bright_city_applications USING gin (drawings_urls jsonb_path_ops);

-- ============================================================
-- 12) Аудит действий администратора
-- ============================================================
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id              BIGSERIAL PRIMARY KEY,
    admin_id        BIGINT NOT NULL,
    admin_platform  TEXT NOT NULL,
    action          TEXT NOT NULL,
    target_platform TEXT,
    target_id       BIGINT,
    params          JSONB,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_admin   ON admin_audit_log (admin_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_target  ON admin_audit_log (target_platform, target_id);

-- Авторство ответов администратора
ALTER TABLE bot_messages ADD COLUMN IF NOT EXISTS admin_id BIGINT;

-- ============================================================
-- 13) Версионирование миграций
-- ============================================================
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL DEFAULT NOW()
);
INSERT INTO schema_migrations (version) VALUES ('M0_security_migration')
    ON CONFLICT (version) DO NOTHING;

COMMIT;

-- ============================================================
-- ПРОВЕРКА после применения:
--   SELECT platform, count(*) FROM bright_city_applications GROUP BY 1;
--   SELECT conname FROM pg_constraint WHERE conname LIKE 'chk_%' OR conname LIKE 'fk_%';
--   SELECT indexname FROM pg_indexes WHERE tablename = 'media_files';
-- ============================================================
