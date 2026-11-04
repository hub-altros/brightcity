BEGIN;
CREATE TABLE IF NOT EXISTS web_admin_settings (
    admin_id                     BIGINT PRIMARY KEY
                                 REFERENCES web_admins(id) ON DELETE CASCADE,
    notify_applications          BOOLEAN NOT NULL DEFAULT TRUE,
    receive_user_messages        BOOLEAN NOT NULL DEFAULT TRUE,
    show_media_in_applications   BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at                   TIMESTAMP NOT NULL DEFAULT NOW()
);
INSERT INTO schema_migrations (version) VALUES ('M2_web_admin_settings')
    ON CONFLICT (version) DO NOTHING;
COMMIT;