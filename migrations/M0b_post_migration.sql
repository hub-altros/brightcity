-- migrations/M0b_post_migration.sql
-- Доводка после M0_security_migration.sql.
--
-- Работает и в psql, и в GUI (pgAdmin / DBeaver / Navicat): НЕ использует
-- мета-команды psql (\echo и т.п.).
--
-- Запуск:
--   psql "$env:DB_NAME" -v ON_ERROR_STOP=1 -f migrations/M0b_post_migration.sql
-- либо просто открыть файл в GUI и выполнить целиком.
--
-- ВАЖНО: последний запрос возвращает ОДНУ таблицу со всеми проверками и
-- ВСЕГДА непустой. Если ваш инструмент показывает только последний
-- результат — смотрите именно его: там колонка status, и всё, что не 'OK',
-- выведено первыми строками.
--
-- Идемпотентно.

BEGIN;

-- ============================================================
-- 1) Чистка осиротевших записей
-- ============================================================

-- 1a. БЕЗОПАСНО: переписку не удаляем, только отвязываем от исчезнувшей заявки.
UPDATE bot_messages m
   SET application_id = NULL
 WHERE m.application_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM bright_city_applications a
                    WHERE a.id = m.application_id);

-- 1b. ВНИМАНИЕ: удаляет осиротевшие записи истории статусов.
--     Они не связаны ни с одной существующей заявкой (заявку удалили,
--     например по 152-ФЗ), практической ценности не несут, но формально
--     это данные аудита.
--     Хотите сохранить — закомментируйте этот DELETE: тогда FK на
--     application_status_history не создастся (это допустимо, код на
--     него не опирается), а проверка ниже покажет ОТСУТСТВУЕТ по fk_ash_app.
DELETE FROM application_status_history h
 WHERE NOT EXISTS (SELECT 1 FROM bright_city_applications a
                    WHERE a.id = h.application_id);

-- ============================================================
-- 2) Внешние ключи
-- ============================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_ash_app') THEN
        ALTER TABLE application_status_history
            ADD CONSTRAINT fk_ash_app FOREIGN KEY (application_id)
            REFERENCES bright_city_applications(id) ON DELETE CASCADE;
        RAISE NOTICE 'fk_ash_app создан';
    ELSE
        RAISE NOTICE 'fk_ash_app уже существует';
    END IF;
EXCEPTION WHEN foreign_key_violation THEN
    RAISE WARNING 'fk_ash_app НЕ создан: остались орфаны в application_status_history';
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_bm_app') THEN
        ALTER TABLE bot_messages
            ADD CONSTRAINT fk_bm_app FOREIGN KEY (application_id)
            REFERENCES bright_city_applications(id) ON DELETE SET NULL;
        RAISE NOTICE 'fk_bm_app создан';
    ELSE
        RAISE NOTICE 'fk_bm_app уже существует';
    END IF;
EXCEPTION WHEN foreign_key_violation THEN
    RAISE WARNING 'fk_bm_app НЕ создан: остались орфаны в bot_messages';
END $$;

COMMIT;

-- ============================================================
-- 3) ЕДИНАЯ ПРОВЕРКА (этот запрос — последний, его и смотрите)
--    ВСЕГДА возвращает строки. Колонка status:
--      OK          — всё на месте
--      ОТСУТСТВУЕТ — объект отсутствует, соответствующий код упадёт
--      число       — для строк kind='orphans': 0 = чисто, >0 = орфаны
--    Всё, что не OK, отсортировано в начало.
-- ============================================================
WITH req(kind, ident, required_by) AS (VALUES
    ('unique_index', 'uq_mf_owner_url',
     'register_media_file: ON CONFLICT (user_platform,user_id,url) — КРИТИЧНО'),
    ('unique_index', 'uq_bca_client_request',
     'save_application_to_db: ON CONFLICT (client_request_id)'),
    ('unique_index', 'uq_gdpr_requests_pending',
     'один незакрытый запрос ПДн на пользователя'),
    ('unique_index', 'uq_outbox_idem',
     'enqueue_outbox: идемпотентность'),
    ('index',        'idx_mf_source_file',
     'поиск по source_file_id (не уникальный — так и задумано)'),
    ('table',        'gdpr_erasure_log',
     'is_user_erased: запрет воскрешения ПДн из кэша'),
    ('table',        'admin_audit_log',
     'log_admin_action: аудит действий администратора'),
    ('table',        'schema_migrations',
     'версионирование миграций'),
    ('column',       'media_files.doc_id',
     'очередь удаления документов (квитанций) с VK CDN'),
    ('column',       'media_files.full_relay_last_attempt_at',
     'fetch_pending_full_relay: окно ретраев'),
    ('column',       'outbox_messages.status',
     'fetch_outbox_batch / mark_outbox_sent'),
    ('column',       'outbox_messages.attempts',
     'fetch_outbox_batch: количество попыток'),
    ('column',       'outbox_messages.claimed_at',
     'fetch_outbox_batch: аренда строки'),
    ('column',       'bright_city_applications.client_request_id',
     'save_application_to_db: защита от дублей'),
    ('column',       'bright_city_applications.consent_at',
     'save_application_to_db: факт согласия (152-ФЗ)'),
    ('column',       'bright_city_applications.anonymized_at',
     'anonymize_user_data'),
    ('column',       'bot_messages.admin_id',
     'авторство ответов администратора'),
    ('constraint',   'fk_bm_app',
     'ссылочная целостность bot_messages'),
    ('constraint',   'fk_ash_app',
     'ссылочная целостность application_status_history')
),
res AS (
    SELECT kind, ident,
           CASE
             WHEN kind = 'unique_index' AND EXISTS (
                    SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                     WHERE c.relname = ident AND i.indisunique) THEN 'OK'
             WHEN kind = 'index' AND EXISTS (
                    SELECT 1 FROM pg_indexes
                     WHERE schemaname = current_schema() AND indexname = ident) THEN 'OK'
             WHEN kind = 'table' AND EXISTS (
                    SELECT 1 FROM information_schema.tables
                     WHERE table_schema = current_schema() AND table_name = ident) THEN 'OK'
             WHEN kind = 'column' AND EXISTS (
                    SELECT 1 FROM information_schema.columns
                     WHERE table_schema = current_schema()
                       AND table_name = split_part(ident, '.', 1)
                       AND column_name = split_part(ident, '.', 2)) THEN 'OK'
             WHEN kind = 'constraint' AND EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = ident) THEN 'OK'
             ELSE 'ОТСУТСТВУЕТ'
           END AS status,
           required_by
      FROM req
),
orph AS (
    SELECT 'orphans'::text AS kind,
           'application_status_history'::text AS ident,
           CASE WHEN cnt = 0 THEN 'OK' ELSE cnt::text END AS status,
           'осиротевшие строки истории статусов (0 = чисто, блокирует fk_ash_app)'::text AS required_by
      FROM (SELECT count(*) AS cnt FROM application_status_history h
             WHERE NOT EXISTS (SELECT 1 FROM bright_city_applications a
                                WHERE a.id = h.application_id)) t
    UNION ALL
    SELECT 'orphans',
           'bot_messages.application_id',
           CASE WHEN cnt = 0 THEN 'OK' ELSE cnt::text END,
           'осиротевшие ссылки в переписке (0 = чисто, блокирует fk_bm_app)'
      FROM (SELECT count(*) AS cnt FROM bot_messages m
             WHERE m.application_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM bright_city_applications a
                                WHERE a.id = m.application_id)) t
    UNION ALL
    SELECT 'extra',
           'уникальный индекс по source_file_id (не должно быть)',
           CASE WHEN cnt = 0 THEN 'OK' ELSE cnt::text END,
           'если >0 — выполните: DROP INDEX IF EXISTS uq_mf_source_file;'
      FROM (SELECT count(*) AS cnt
              FROM pg_index i
              JOIN pg_class c ON c.oid = i.indexrelid
              JOIN pg_class tb ON tb.oid = i.indrelid
             WHERE tb.relname = 'media_files' AND i.indisunique
               AND pg_get_indexdef(i.indexrelid) LIKE '%(source_file_id)%') t
)
SELECT kind, ident, status, required_by
  FROM (SELECT kind, ident, status, required_by FROM res
        UNION ALL
        SELECT kind, ident, status, required_by FROM orph) all_checks
 ORDER BY (status = 'OK'), kind, ident;
