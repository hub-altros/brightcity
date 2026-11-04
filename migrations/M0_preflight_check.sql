-- migrations/M0_preflight_check.sql
-- Предполётная проверка ПЕРЕД применением M0_security_migration.sql.
-- Ничего не меняет: только показывает, что помешает миграции.
--
-- Работает и в psql, и в GUI (pgAdmin / DBeaver / Navicat):
-- мета-команды psql (\echo) не используются.
--
-- Последний запрос возвращает ОДНУ таблицу-сводку и ВСЕГДА непустую.
-- Если ваш инструмент показывает только последний результат — смотрите его:
-- колонка «количество» должна быть 0 везде, кроме дублей source_file_id
-- (это норма).

-- 1. Дубли незакрытых GDPR-запросов — чистится миграцией автоматически.
SELECT platform, user_id, count(*) AS pending_rows
  FROM gdpr_requests
 WHERE fulfilled = FALSE
 GROUP BY platform, user_id
HAVING count(*) > 1
 ORDER BY pending_rows DESC
 LIMIT 20;

-- 2. Дубли source_file_id — это НОРМА (один TG-файл релаится повторно).
--    Уникальный индекс по этому полю больше не создаётся.
SELECT source_file_id, count(*) AS copies
  FROM media_files
 WHERE source_file_id IS NOT NULL
 GROUP BY source_file_id
HAVING count(*) > 1
 ORDER BY copies DESC
 LIMIT 10;

-- 3. Дубли (user_platform, user_id, url) — мешают uq_mf_owner_url.
--    В норме пусто: колонка url объявлена UNIQUE.
SELECT user_platform, user_id, url, count(*) AS copies
  FROM media_files
 GROUP BY user_platform, user_id, url
HAVING count(*) > 1
 ORDER BY copies DESC
 LIMIT 20;

-- 4. Недопустимые значения platform — мешают CHECK.
--    Допустимы только vk / tg / max в нижнем регистре.
SELECT 'bright_city_applications' AS tbl, platform, count(*) FROM bright_city_applications GROUP BY platform
UNION ALL SELECT 'bot_user_states', platform, count(*) FROM bot_user_states GROUP BY platform
UNION ALL SELECT 'bot_messages', platform, count(*) FROM bot_messages GROUP BY platform
UNION ALL SELECT 'banned_users', platform, count(*) FROM banned_users GROUP BY platform
UNION ALL SELECT 'admin_users', platform, count(*) FROM admin_users GROUP BY platform
UNION ALL SELECT 'admin_settings', platform, count(*) FROM admin_settings GROUP BY platform
UNION ALL SELECT 'gdpr_requests', platform, count(*) FROM gdpr_requests GROUP BY platform
UNION ALL SELECT 'media_files', user_platform, count(*) FROM media_files GROUP BY user_platform
 ORDER BY 1, 2;

-- ============================================================
-- 5. СВОДКА (последний запрос — его и смотрите)
--    «количество» = 0 → проблем нет.
-- ============================================================
SELECT 'дубли незакрытых GDPR-запросов' AS проблема,
       (SELECT count(*) FROM (SELECT 1 FROM gdpr_requests
                               WHERE fulfilled = FALSE
                               GROUP BY platform, user_id
                              HAVING count(*) > 1) x) AS количество,
       'чистится миграцией автоматически' AS что_делать
UNION ALL
SELECT 'дубли (user_platform,user_id,url)',
       (SELECT count(*) FROM (SELECT 1 FROM media_files
                               GROUP BY user_platform, user_id, url
                              HAVING count(*) > 1) x),
       'чистится миграцией автоматически'
UNION ALL
SELECT 'дубли source_file_id (это норма)',
       (SELECT count(*) FROM (SELECT 1 FROM media_files
                               WHERE source_file_id IS NOT NULL
                               GROUP BY source_file_id
                              HAVING count(*) > 1) x),
       'ничему не мешает; уникальный индекс не создаётся'
UNION ALL
SELECT 'орфаны application_status_history',
       (SELECT count(*) FROM application_status_history h
         WHERE NOT EXISTS (SELECT 1 FROM bright_city_applications a
                            WHERE a.id = h.application_id)),
       'блокирует FK; чистится M0b_post_migration.sql'
UNION ALL
SELECT 'орфаны bot_messages.application_id',
       (SELECT count(*) FROM bot_messages m
         WHERE m.application_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM bright_city_applications a
                            WHERE a.id = m.application_id)),
       'блокирует FK; чистится M0b_post_migration.sql'
UNION ALL
SELECT 'недопустимые значения platform',
       (SELECT count(*) FROM (
            SELECT platform AS p FROM bright_city_applications WHERE platform NOT IN ('vk','tg','max')
            UNION ALL SELECT platform FROM bot_user_states WHERE platform NOT IN ('vk','tg','max')
            UNION ALL SELECT platform FROM bot_messages WHERE platform NOT IN ('vk','tg','max')
            UNION ALL SELECT platform FROM banned_users WHERE platform NOT IN ('vk','tg','max')
            UNION ALL SELECT platform FROM admin_users WHERE platform NOT IN ('vk','tg','max')
            UNION ALL SELECT platform FROM admin_settings WHERE platform NOT IN ('vk','tg','max')
            UNION ALL SELECT platform FROM gdpr_requests WHERE platform NOT IN ('vk','tg','max')
            UNION ALL SELECT user_platform FROM media_files WHERE user_platform NOT IN ('vk','tg','max')
       ) x),
       'CHECK не пройдёт VALIDATE; поправить вручную'
UNION ALL
SELECT 'миграция уже применена (колонки/таблицы)',
       (SELECT count(*) FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND ((table_name = 'media_files' AND column_name = 'doc_id')
             OR (table_name = 'outbox_messages' AND column_name = 'status'))),
       '2 = применена; 0 = не применена (норма перед запуском)';
