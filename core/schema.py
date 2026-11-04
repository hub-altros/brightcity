# core/schema.py
"""Схема БД: единое место для CREATE TABLE и CREATE INDEX.

Все таблицы платформо-зависимые — ключи включают platform ('vk', 'tg', 'max').

Использование:
  - При первом деплое / после миграции: ensure_schema() создаёт всё, что нужно.
  - При явной миграции старой БД: scripts/migrate_to_platform.py вызывает
    drop_all_tables() + ensure_schema().
"""
import logging

logger = logging.getLogger("bright_city_bot")


# --- DROP (для миграции) ---

def drop_all_tables(cur):
    """Удаляет все таблицы бота в правильном порядке (FK-безопасно).
    Вызывается ТОЛЬКО из scripts/migrate_to_platform.py.
    """
    cur.execute("""
        DROP TABLE IF EXISTS
            gdpr_completion_notified,
            schema_migrations,
            admin_audit_log,
            gdpr_erasure_log,
            gdpr_audit_log,
            gdpr_requests,
            media_files,
            outbox_messages,
            admin_sudo_sessions,
            admin_settings,
            admin_users,
            banned_users,
            bot_messages,
            bot_user_states,
            application_status_history,
            bright_city_applications
        CASCADE
    """)
    logger.warning("Все таблицы бота удалены (DROP CASCADE)")


# --- CREATE ---

def ensure_schema(cur):
    """Создаёт все таблицы и индексы. Идемпотентно.

    Перед вызовом устанавливает lock_timeout = 30s, чтобы при
    одновременном старте VK- и TG-процессов один подождал другого.
    """
    cur.execute("SET LOCAL lock_timeout = '30s'")
    cur.execute("SET LOCAL statement_timeout = '60s'")

    # --- Заявки ---
    # ФИКС SCHEMA-1: добавлены consent_* / anonymized_at / client_request_id.
    # ФИКС SCHEMA-2: user_id теперь NULL-able (анонимизация его обнуляет).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bright_city_applications (
            id                    BIGSERIAL PRIMARY KEY,
            platform              TEXT NOT NULL DEFAULT 'vk'
                                  CHECK (platform IN ('vk','tg','max')),
            user_id               BIGINT,
            location              TEXT,
            placement_location    TEXT,
            user_type             TEXT,
            phone                 TEXT,
            name_age              TEXT,
            child_photo_urls      JSONB DEFAULT '[]'::jsonb,
            format_choice         TEXT,
            drawings_urls         JSONB DEFAULT '[]'::jsonb,
            training_info         TEXT,
            payment_doc_urls      JSONB DEFAULT '[]'::jsonb,
            org_info_attachments  JSONB DEFAULT '[]'::jsonb,
            status                TEXT NOT NULL DEFAULT 'new',
            banner_url            TEXT,
            rejection_reason      TEXT,
            created_at            TIMESTAMP DEFAULT NOW(),
            assigned_admin_id     BIGINT,
            assigned_admin_name   TEXT,
            opened_by_admin_id    BIGINT,
            opened_by_admin_name  TEXT,
            opened_at             TIMESTAMP,
            status_changed_by_name TEXT,
            status_changed_at     TIMESTAMP,
            -- ФИКС P1.3 (152-ФЗ)
            consent_version       TEXT,
            consent_text_hash     TEXT,
            consent_at            TIMESTAMP,
            consent_platform      TEXT,
            -- ФИКС GDPR-анонимизации
            anonymized_at         TIMESTAMP,
            -- ФИКС идемпотентности повторного нажатия «Отправить заявку»
            client_request_id     TEXT
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_bca_platform_user
        ON bright_city_applications(platform, user_id)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_bca_platform_status
        ON bright_city_applications(platform, status)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_bca_platform_created
        ON bright_city_applications(platform, created_at)
    """)
    # ФИКС: уникальный частичный индекс — ON CONFLICT (client_request_id)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_bca_client_request
        ON bright_city_applications (client_request_id)
        WHERE client_request_id IS NOT NULL
    """)
    # GIN-индексы для JSONB-поиска (обратная совместимость с M0)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_bca_child_photos_gin
        ON bright_city_applications USING gin (child_photo_urls jsonb_path_ops)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_bca_drawings_gin
        ON bright_city_applications USING gin (drawings_urls jsonb_path_ops)
    """)

    # --- История статусов ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS application_status_history (
            id              BIGSERIAL PRIMARY KEY,
            application_id  BIGINT NOT NULL,
            old_status      TEXT,
            new_status      TEXT NOT NULL,
            admin_id        BIGINT,
            admin_name      TEXT,
            admin_platform  TEXT,
            changed_at      TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_ash_app
        ON application_status_history(application_id)
    """)
    # FK добавляется в ensure_schema как NOT VALID + VALIDATE,
    # чтобы не падать на существующих орфанах (см. M0b).
    cur.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_ash_app') THEN
                ALTER TABLE application_status_history
                    ADD CONSTRAINT fk_ash_app FOREIGN KEY (application_id)
                    REFERENCES bright_city_applications(id) ON DELETE CASCADE
                    NOT VALID;
            END IF;
            BEGIN
                ALTER TABLE application_status_history VALIDATE CONSTRAINT fk_ash_app;
            EXCEPTION WHEN others THEN
                RAISE WARNING 'fk_ash_app НЕ VALIDATED: остались орфаны';
            END;
        END $$;
    """)

    # --- Состояния пользователей ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_user_states (
            platform    TEXT NOT NULL DEFAULT 'vk'
                        CHECK (platform IN ('vk','tg','max')),
            user_id     BIGINT NOT NULL,
            state       TEXT NOT NULL DEFAULT 'start',
            temp_data   JSONB DEFAULT '{}'::jsonb,
            updated_at  TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (platform, user_id)
        )
    """)

    # --- История сообщений ---
    # ФИКС: admin_id для авторства ответов администратора.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_messages (
            id              BIGSERIAL PRIMARY KEY,
            platform        TEXT NOT NULL DEFAULT 'vk'
                            CHECK (platform IN ('vk','tg','max')),
            user_id         BIGINT NOT NULL,
            direction       TEXT NOT NULL,
            text            TEXT,
            application_id  BIGINT,
            admin_id        BIGINT,
            created_at      TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_bm_platform_user_created
        ON bot_messages(platform, user_id, created_at)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_bm_application
        ON bot_messages(application_id)
    """)
    cur.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_bm_app') THEN
                ALTER TABLE bot_messages
                    ADD CONSTRAINT fk_bm_app FOREIGN KEY (application_id)
                    REFERENCES bright_city_applications(id) ON DELETE SET NULL
                    NOT VALID;
            END IF;
            BEGIN
                ALTER TABLE bot_messages VALIDATE CONSTRAINT fk_bm_app;
            EXCEPTION WHEN others THEN
                RAISE WARNING 'fk_bm_app НЕ VALIDATED: остались орфаны';
            END;
        END $$;
    """)

    # --- Банлист ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS banned_users (
            platform             TEXT NOT NULL DEFAULT 'vk'
                                 CHECK (platform IN ('vk','tg','max')),
            user_id              BIGINT NOT NULL,
            banned_by            BIGINT,
            banned_by_platform   TEXT,
            banned_at            TIMESTAMP DEFAULT NOW(),
            expires_at           TIMESTAMP,
            reason               TEXT,
            ban_count            INTEGER DEFAULT 0,
            PRIMARY KEY (platform, user_id)
        )
    """)

    # --- Динамические админы ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_users (
            platform    TEXT NOT NULL DEFAULT 'vk'
                        CHECK (platform IN ('vk','tg','max')),
            admin_id    BIGINT NOT NULL,
            added_by    BIGINT,
            added_at    TIMESTAMP DEFAULT NOW(),
            is_active   BOOLEAN DEFAULT TRUE,
            PRIMARY KEY (platform, admin_id)
        )
    """)

    # --- Настройки админов ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_settings (
            platform                TEXT NOT NULL DEFAULT 'vk'
                                    CHECK (platform IN ('vk','tg','max')),
            admin_id                BIGINT NOT NULL,
            notify_applications     BOOLEAN DEFAULT TRUE,
            receive_user_messages   BOOLEAN DEFAULT TRUE,
            PRIMARY KEY (platform, admin_id)
        )
    """)

    # --- Sudo-сессии ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_sudo_sessions (
            request_id  TEXT PRIMARY KEY,
            platform    TEXT NOT NULL DEFAULT 'vk',
            admin_id    BIGINT NOT NULL,
            action      TEXT NOT NULL,
            target      TEXT,
            params      JSONB,
            created_at  TIMESTAMP DEFAULT NOW(),
            expires_at  TIMESTAMP NOT NULL
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_ass_platform_admin
        ON admin_sudo_sessions(platform, admin_id)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_ass_expires
        ON admin_sudo_sessions(expires_at)
    """)

    # --- Медиафайлы ---
    # ФИКС SCHEMA-3: UNIQUE(url) заменён на UNIQUE(user_platform, user_id, url),
    # т.к. register_media_file делает ON CONFLICT (user_platform, user_id, url).
    # ФИКС: добавлены doc_id и full_relay_last_attempt_at.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS media_files (
            id                    BIGSERIAL PRIMARY KEY,
            url                   TEXT NOT NULL,
            owner_id              BIGINT,
            photo_id              BIGINT,
            doc_id                BIGINT,
            kind                  TEXT,
            user_platform         TEXT NOT NULL
                                  CHECK (user_platform IN ('vk','tg','max')),
            user_id               BIGINT NOT NULL,
            application_id        BIGINT,
            uploaded_at           TIMESTAMP DEFAULT NOW(),
            deleted_at            TIMESTAMP,
            vk_delete_confirmed   BOOLEAN DEFAULT FALSE,
            vk_delete_attempts    INTEGER DEFAULT 0,
            vk_delete_last_at     TIMESTAMP,
            vk_delete_last_error  TEXT,
            source_file_id        TEXT,
            source_platform       TEXT,
            full_relay_requested_at TIMESTAMP,
            full_relay_confirmed  BOOLEAN DEFAULT FALSE,
            full_relay_url        TEXT,
            full_relay_owner_id   BIGINT,
            full_relay_photo_id   BIGINT,
            full_relay_attempts   INTEGER DEFAULT 0,
            full_relay_last_error TEXT,
            full_relay_last_attempt_at TIMESTAMP,
            vk_url                TEXT,
            vk_owner_id           BIGINT,
            vk_photo_id           BIGINT,
            vk_doc_id             BIGINT,
            CONSTRAINT uq_mf_owner_url UNIQUE (user_platform, user_id, url)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_media_files_user ON media_files(user_platform, user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_media_files_app ON media_files(application_id)")
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_media_files_pending_delete
        ON media_files(deleted_at, vk_delete_confirmed, vk_delete_last_at)
        WHERE deleted_at IS NOT NULL AND vk_delete_confirmed = FALSE
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_media_files_full_relay_pending
        ON media_files(full_relay_requested_at)
        WHERE full_relay_requested_at IS NOT NULL
          AND full_relay_confirmed = FALSE
          AND source_file_id IS NOT NULL
    """)
    # НЕ уникальный — по комментарию в M0.
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_mf_source_file
        ON media_files (source_file_id) WHERE source_file_id IS NOT NULL
    """)

    # --- GDPR аудит ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS gdpr_audit_log (
            id              BIGSERIAL PRIMARY KEY,
            admin_id        BIGINT NOT NULL,
            admin_platform  TEXT NOT NULL,
            target_platform TEXT NOT NULL,
            target_user_id  BIGINT NOT NULL,
            action          TEXT NOT NULL,
            details         JSONB,
            created_at      TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_gdpr_audit_created ON gdpr_audit_log(created_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_gdpr_audit_target ON gdpr_audit_log(target_platform, target_user_id)")

    # --- GDPR-запросы пользователей ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS gdpr_requests (
            id              BIGSERIAL PRIMARY KEY,
            platform        TEXT NOT NULL
                            CHECK (platform IN ('vk','tg','max')),
            user_id         BIGINT NOT NULL,
            requested_at    TIMESTAMP DEFAULT NOW(),
            fulfilled       BOOLEAN DEFAULT FALSE,
            fulfilled_at    TIMESTAMP,
            fulfilled_by    BIGINT
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_gdpr_requests_user
        ON gdpr_requests(platform, user_id, fulfilled)
    """)
    # Один незакрытый запрос на пользователя (частичный уникальный индекс).
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_gdpr_requests_pending
        ON gdpr_requests (platform, user_id) WHERE fulfilled = FALSE
    """)

    # --- Очередь исходящих ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS outbox_messages (
            id              BIGSERIAL PRIMARY KEY,
            platform        TEXT NOT NULL,
            target_user_id  BIGINT NOT NULL,
            message_type    TEXT NOT NULL DEFAULT 'text',
            text            TEXT,
            keyboard        JSONB,
            attachment      TEXT,
            created_at      TIMESTAMP DEFAULT NOW(),
            sent_at         TIMESTAMP,
            error           TEXT,
            status          TEXT NOT NULL DEFAULT 'pending',
            claimed_at      TIMESTAMP,
            attempts        INTEGER NOT NULL DEFAULT 0,
            idempotency_key TEXT
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_om_pending
        ON outbox_messages(platform, created_at)
        WHERE sent_at IS NULL
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_om_pending_status
        ON outbox_messages(platform, created_at)
        WHERE status = 'pending'
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_outbox_pending
        ON outbox_messages (platform, status, created_at)
        WHERE status IN ('pending', 'in_progress')
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_outbox_idem
        ON outbox_messages (idempotency_key) WHERE idempotency_key IS NOT NULL
    """)

    # --- Журнал стирания ПДн ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS gdpr_erasure_log (
            platform   TEXT NOT NULL
                       CHECK (platform IN ('vk','tg','max')),
            user_id    BIGINT NOT NULL,
            erased_at  TIMESTAMP NOT NULL DEFAULT NOW(),
            admin_id   BIGINT,
            PRIMARY KEY (platform, user_id)
        )
    """)

    # --- Финальные уведомления об удалении ПДн ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS gdpr_completion_notified (
            platform    TEXT NOT NULL,
            user_id     BIGINT NOT NULL,
            notified_at TIMESTAMP NOT NULL DEFAULT NOW(),
            PRIMARY KEY (platform, user_id)
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_gdpr_completion_notified_at
        ON gdpr_completion_notified(notified_at DESC)
    """)

    # --- Аудит действий администратора ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_audit_log (
            id              BIGSERIAL PRIMARY KEY,
            admin_id        BIGINT NOT NULL,
            admin_platform  TEXT NOT NULL,
            action          TEXT NOT NULL,
            target_platform TEXT,
            target_id       BIGINT,
            params          JSONB,
            created_at      TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_created "
                "ON admin_audit_log(created_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_admin "
                "ON admin_audit_log(admin_id, created_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_target "
                "ON admin_audit_log(target_platform, target_id)")
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_admin_audit_remind
        ON admin_audit_log (target_platform, target_id, created_at DESC)
        WHERE action = 'remind_incomplete'
    """)

    # --- Версионирование миграций ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT PRIMARY KEY,
            applied_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("""
        INSERT INTO schema_migrations (version)
        VALUES ('M0_security_migration')
        ON CONFLICT (version) DO NOTHING
    """)

    logger.info("Схема БД актуальна (все таблицы и индексы созданы/проверены)")


def check_schema_compatibility(cur):
    """Проверяет, что БД поддерживает мультиплатформенную схему.
    Бросает RuntimeError, если схема старая.
    """
    _REQUIRED_COLUMNS = {
        "media_files": ("url", "user_platform", "user_id", "kind",
                        "doc_id", "full_relay_last_attempt_at",
                        "vk_url", "vk_owner_id", "vk_photo_id", "vk_doc_id"),
        "bright_city_applications": ("platform", "user_id",
                                     "consent_at", "anonymized_at",
                                     "client_request_id"),
        "outbox_messages": ("status", "attempts", "claimed_at"),
        "gdpr_erasure_log": ("platform", "user_id"),
        "gdpr_completion_notified": ("platform", "user_id"),
    }
    for _table, _cols in _REQUIRED_COLUMNS.items():
        for _col in _cols:
            cur.execute("""
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = %s AND column_name = %s
            """, (_table, _col))
            if cur.fetchone() is None:
                raise RuntimeError(
                    "Схема несовместима: нет {}.{}.\n"
                    "Примените migrations/M0_security_migration.sql и "
                    "migrations/M0b_post_migration.sql, затем перезапустите бота."
                    .format(_table, _col))

    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'bot_user_states' AND column_name = 'platform'
    """)
    has_platform = cur.fetchone() is not None
    if not has_platform:
        raise RuntimeError(
            "Схема БД устарела: в bot_user_states нет колонки platform.\n"
            "Запустите миграцию:\n"
            "    python scripts/migrate_to_platform.py --force\n"
            "Затем перезапустите бота."
        )