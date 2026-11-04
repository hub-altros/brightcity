# core/storage.py
"""Хранилище: состояние пользователя, история сообщений, заявки.

Всё, что касается чтения/записи данных в БД и не является бизнес-логикой.
Платформо-агностично: каждый запрос фильтруется по platform() (кроме
мест, где нужен явный target_platform).
"""
import json
import logging
import threading
import time

from core.db import (
    get_db_connection, return_db_connection,
    SafeJson, safe_str, is_user_erased,
)
from core.limits import MAX_PAYLOAD_LEN
from core.runtime import platform
from core.texts import STATE_START


logger = logging.getLogger("bright_city_bot")


# ============================================================
#  СОСТОЯНИЕ ПОЛЬЗОВАТЕЛЯ
# ============================================================

class UserState:
    def __init__(self):
        self._cache_states = {}
        self._cache_temp = {}
        self._locks = {}
        self._locks_guard = threading.Lock()
        self._locks_cleanup_last = 0.0
        self._MAX_LOCKS = 5000

        # Пункт 20: локальный кэш «пользователь стёрт» на уровне UserState.
        # Отдельный от is_user_erased (в core.db), чтобы избежать SQL-запроса
        # на каждое сохранение. TTL длиннее, чем в core.db (там 60 с), потому
        # что данные о стирании меняются редко. Инвалидируется в invalidate().
        self._erased_local = {}            # (platform, user_id) -> (erased, ts)
        self._erased_local_ttl = 300.0
        self._erased_local_lock = threading.Lock()

        # ФИКС Баг-14: ограничение размера in-memory кэшей.
        # Раньше _cache_states/_cache_temp/_erased_local росли линейно
        # за всё время жизни процесса — утечка на боте с большим
        # потоком пользователей. _locks чистится (см. _cleanup_locks),
        # эти три — нет.
        self._MAX_CACHE_ENTRIES = 10000
        self._cache_cleanup_last = 0.0
        self._cache_cleanup_interval = 300.0   # 5 минут

    def _cleanup_caches(self):
        """Пункт 14: не даём _cache_states/_cache_temp/_erased_local
        расти бесконечно.

        Стратегия: при превышении лимита — полная очистка словаря.
        Это безопасно: следующий get()/get_temp() перечитает данные
        из БД. Пик трафика на БД от этого — единичные SELECT'ы на
        «горячих» пользователей, зато кэш не жрёт память.

        Вызывается из _get_lock, но реально чистит не чаще, чем раз
        в _cache_cleanup_interval.
        """
        now = time.time()
        if now - self._cache_cleanup_last < self._cache_cleanup_interval:
            # Быстрый выход без локов, если недавно уже чистили.
            return
        self._cache_cleanup_last = now

        # _cache_states/_cache_temp — под _locks_guard (та же защита,
        # что у _locks и в _save).
        with self._locks_guard:
            if len(self._cache_states) >= self._MAX_CACHE_ENTRIES:
                logger.warning(
                    "UserState._cache_states превысил %d — очищаю "
                    "(перечитается из БД при следующем get)",
                    self._MAX_CACHE_ENTRIES)
                self._cache_states.clear()
            if len(self._cache_temp) >= self._MAX_CACHE_ENTRIES:
                logger.warning(
                    "UserState._cache_temp превысил %d — очищаю",
                    self._MAX_CACHE_ENTRIES)
                self._cache_temp.clear()

        # _erased_local — под своим локом.
        with self._erased_local_lock:
            if len(self._erased_local) >= self._MAX_CACHE_ENTRIES:
                logger.warning(
                    "UserState._erased_local превысил %d — очищаю",
                    self._MAX_CACHE_ENTRIES)
                self._erased_local.clear()

    def _cleanup_locks(self, now):
        if len(self._locks) > self._MAX_LOCKS and now - self._locks_cleanup_last > 300:
            stale_keys = []
            for key, lock in list(self._locks.items()):
                if lock.acquire(blocking=False):
                    stale_keys.append(key)
            for key in stale_keys:
                lock = self._locks.get(key)
                if lock is not None:
                    lock.release()
                    del self._locks[key]
            self._locks_cleanup_last = now

    def _get_lock(self, user_id):
        key = (platform(), user_id)
        now = time.time()
        with self._locks_guard:
            self._cleanup_locks(now)
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
        # ФИКС Баг-14: чистим остальные кэши.
        self._cleanup_caches()
        lock.acquire()
        return lock

    # --- Пункт 20: локальная проверка «стёрт» ---

    def _is_erased_fast(self, pl, user_id):
        key = (pl, user_id)
        now = time.time()
        with self._erased_local_lock:
            hit = self._erased_local.get(key)
        if hit is not None and now - hit[1] < self._erased_local_ttl:
            return hit[0]
        # Промах локального кэша — идём в core.db.is_user_erased,
        # у которого свой 60-секундный кэш (то есть реальный SQL
        # выполняется не чаще, чем раз в 60 с).
        erased = is_user_erased(pl, user_id)
        with self._erased_local_lock:
            self._erased_local[key] = (erased, now)
        return erased

    def _forget_erased_local(self, pl, user_id):
        with self._erased_local_lock:
            self._erased_local.pop((pl, user_id), None)

    # --- Чтение ---

    def get(self, user_id):
        lock = self._get_lock(user_id)
        try:
            pl = platform()
            cache_key = (pl, user_id)
            if cache_key in self._cache_states:
                return self._cache_states[cache_key]
            state = STATE_START
            conn = None
            try:
                conn = get_db_connection()
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT state FROM bot_user_states "
                        "WHERE platform = %s AND user_id = %s",
                        (pl, user_id))
                    row = cur.fetchone()
                    if row:
                        state = safe_str(row[0])
            except Exception as e:
                logger.error("DB error (get state): %s", e)
            finally:
                if conn:
                    conn.rollback()
                    return_db_connection(conn)
            self._cache_states[cache_key] = state
            return state
        finally:
            lock.release()

    def _load_temp_from_db(self, user_id):
        pl = platform()
        cache_key = (pl, user_id)
        if cache_key not in self._cache_temp:
            temp_data = {}
            conn = None
            try:
                conn = get_db_connection()
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT temp_data FROM bot_user_states "
                        "WHERE platform = %s AND user_id = %s",
                        (pl, user_id))
                    row = cur.fetchone()
                    if row and row[0]:
                        if isinstance(row[0], dict):
                            temp_data = row[0]
                        elif isinstance(row[0], str):
                            temp_data = json.loads(row[0])
                        else:
                            temp_data = json.loads(safe_str(row[0]))
            except Exception as e:
                logger.error("DB error (load temp from db): %s", e)
            finally:
                if conn:
                    conn.rollback()
                    return_db_connection(conn)
            self._cache_temp[cache_key] = temp_data

    def _ensure_state_cached(self, user_id):
        pl = platform()
        cache_key = (pl, user_id)
        if cache_key in self._cache_states:
            return
        state = STATE_START
        conn = None
        try:
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT state FROM bot_user_states "
                    "WHERE platform = %s AND user_id = %s",
                    (pl, user_id))
                row = cur.fetchone()
                if row:
                    state = safe_str(row[0])
        except Exception as e:
            logger.error("DB error (ensure state cached): %s", e)
        finally:
            if conn:
                conn.rollback()
                return_db_connection(conn)
        self._cache_states[cache_key] = state

    def set(self, user_id, state):
        lock = self._get_lock(user_id)
        try:
            self._load_temp_from_db(user_id)
            pl = platform()
            cache_key = (pl, user_id)
            self._cache_states[cache_key] = state
            self._save(user_id, state, self._cache_temp.get(cache_key, {}))
        finally:
            lock.release()

    def get_temp(self, user_id):
        lock = self._get_lock(user_id)
        try:
            pl = platform()
            cache_key = (pl, user_id)
            if cache_key in self._cache_temp:
                data = self._cache_temp[cache_key]
                return json.loads(json.dumps(data, ensure_ascii=False)) if data else {}
            temp_data = {}
            conn = None
            try:
                conn = get_db_connection()
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT temp_data FROM bot_user_states "
                        "WHERE platform = %s AND user_id = %s",
                        (pl, user_id))
                    row = cur.fetchone()
                    if row and row[0]:
                        if isinstance(row[0], dict):
                            temp_data = row[0]
                        elif isinstance(row[0], str):
                            temp_data = json.loads(row[0])
                        else:
                            temp_data = json.loads(safe_str(row[0]))
            except Exception as e:
                logger.error("DB error (get temp): %s", e)
            finally:
                if conn:
                    conn.rollback()
                    return_db_connection(conn)
            self._cache_temp[cache_key] = temp_data
            return json.loads(json.dumps(temp_data, ensure_ascii=False)) if temp_data else {}
        finally:
            lock.release()

    def set_temp(self, user_id, temp_data):
        lock = self._get_lock(user_id)
        try:
            self._ensure_state_cached(user_id)
            pl = platform()
            cache_key = (pl, user_id)
            self._cache_temp[cache_key] = temp_data
            self._save(user_id, self._cache_states.get(cache_key, STATE_START), temp_data)
        finally:
            lock.release()

    def update_temp(self, user_id, updates):
        lock = self._get_lock(user_id)
        try:
            self._load_temp_from_db(user_id)
            self._ensure_state_cached(user_id)
            pl = platform()
            cache_key = (pl, user_id)
            temp = dict(self._cache_temp.get(cache_key, {}))
            temp.update(updates)
            self._cache_temp[cache_key] = temp
            self._save(user_id, self._cache_states.get(cache_key, STATE_START), temp)
        finally:
            lock.release()

    def clear(self, user_id):
        lock = self._get_lock(user_id)
        try:
            pl = platform()
            conn = None
            try:
                conn = get_db_connection()
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM bot_user_states "
                        "WHERE platform = %s AND user_id = %s",
                        (pl, user_id))
                conn.commit()
            except Exception as e:
                logger.error("DB error (clear state): %s", e)
            finally:
                if conn:
                    conn.rollback()
                    return_db_connection(conn)
            cache_key = (pl, user_id)
            self._cache_states.pop(cache_key, None)
            self._cache_temp.pop(cache_key, None)
        finally:
            lock.release()

    def invalidate(self, user_id, platform_name=None):
        """Сбрасывает ТОЛЬКО in-memory кэш состояния (БД не трогает).

        Пункт 20: также сбрасывает локальный кэш «пользователь стёрт» —
        иначе после GDPR-удаления is_erased_fast вернёт False (устаревшее
        значение), и следующее сообщение пользователя перезапишет
        bot_user_states.
        """
        pl = platform_name or platform()
        with self._locks_guard:
            self._cache_states.pop((pl, user_id), None)
            self._cache_temp.pop((pl, user_id), None)
        # Пункт 20: сброс локального кэша стёртых.
        self._forget_erased_local(pl, user_id)

    def _save(self, user_id, state, temp_data):
        pl = platform()
        # ФИКС Баг-46: НЕ используем локальный _erased_local (TTL 300)
        # — только DB-кэшированный core.db.is_user_erased (TTL 60).
        #
        # Мотивация: локальный кэш 300 сек «переживает» GDPR-удаление,
        # если cache_listener не доставил notify (например, второй
        # процесс ещё не перезапущен с новым кодом, где
        # _VALID_CACHE_KINDS включает "gdpr"). В окне 60–300 сек после
        # удаления _save писал строку в bot_user_states, воскрешая
        # пользователя с пустым temp_data.
        #
        # is_user_erased имеет собственный 60-секундный кэш в core.db,
        # поэтому SQL выполняется не чаще раза в минуту на пользователя.
        # Цена — потеря Python-кэша поверх (был микрооптимизацией), но
        # correctness важнее.
        from core.db import is_user_erased
        if platform == "web":
            return
        if is_user_erased(pl, user_id):
            logger.warning("_save: пропуск записи для стёртого %s/%s", pl, user_id)
            with self._locks_guard:
                self._cache_states.pop((pl, user_id), None)
                self._cache_temp.pop((pl, user_id), None)
            # Сбрасываем и локальный кэш — иначе следующий _is_erased_fast
            # в других путях (get_temp и т.п.) отдаст stale False.
            self._forget_erased_local(pl, user_id)
            return
        conn = None
        try:
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO bot_user_states (platform, user_id, state, temp_data, updated_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (platform, user_id) DO UPDATE SET
                        state = EXCLUDED.state,
                        temp_data = EXCLUDED.temp_data,
                        updated_at = NOW()
                """, (pl, user_id, safe_str(state), SafeJson(temp_data)))
            conn.commit()
        except Exception as e:
            logger.error("DB error (save state): %s", e)
        finally:
            if conn:
                conn.rollback()
                return_db_connection(conn)


# Глобальный singleton
user_state = UserState()


# ============================================================
#  СООБЩЕНИЯ
# ============================================================

def save_message_to_db(user_id, direction, text, application_id=None,
                       target_platform=None, platform_message_id=None,
                       reply_to_platform_message_id=None):
    """Сохраняет сообщение в bot_messages.

    Возвращает bot_message_id (id в bot_messages) или None.

    platform_message_id — id этого сообщения в TG/VK/MAX.
    reply_to_platform_message_id — id сообщения, на которое юзер (или
        админ) сделал нативный reply. Ищется в bot_messages, чтобы
        сохранить reply_to_id / reply_to_text / reply_to_direction —
        для рендера цитаты в админке.

    Если параметры не переданы явно, читаются из thread-local
    контекста (см. core.runtime.get_event_context). Это позволяет
    registration.py / admin.py не пробрасывать их через все сигнатуры.
    """
    from core.runtime import get_event_context

    # Для входящих берём контекст, если не задано явно.
    # Для исходящих (direction='out') reply_to не имеет смысла —
    # это наше собственное сообщение, к нему нет reply.
    if direction == "in":
        ctx_pmi, ctx_rpm = get_event_context()
        if platform_message_id is None:
            platform_message_id = ctx_pmi
        if reply_to_platform_message_id is None:
            reply_to_platform_message_id = ctx_rpm
    else:
        reply_to_platform_message_id = None

    conn = None
    try:
        pl = target_platform or platform()
        reply_to_id = None
        reply_to_text = None
        reply_to_direction = None

        conn = get_db_connection()
        with conn.cursor() as cur:
            # Если есть reply_to_platform_message_id — найдём цитируемое
            # сообщение в bot_messages, чтобы сохранить текст/автора.
            if reply_to_platform_message_id:
                cur.execute(
                    "SELECT id, text, direction FROM bot_messages "
                    "WHERE platform = %s AND user_id = %s "
                    "AND platform_message_id = %s "
                    "ORDER BY created_at DESC LIMIT 1",
                    (pl, user_id, str(reply_to_platform_message_id)))
                orig = cur.fetchone()
                if orig:
                    reply_to_id = orig[0]
                    reply_to_text = (orig[1] or "")[:500]
                    reply_to_direction = orig[2]

            cur.execute(
                "INSERT INTO bot_messages "
                "(platform, user_id, direction, text, application_id, "
                " platform_message_id, reply_to_id, reply_to_text, "
                " reply_to_direction) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (pl, user_id, direction, safe_str(text), application_id,
                 platform_message_id, reply_to_id, reply_to_text,
                 reply_to_direction))
            row = cur.fetchone()
        conn.commit()
        return row[0] if row else None
    except Exception as e:
        logger.error("DB error (messages): %s", e)
        return None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def get_history_for_user(target_user_id, limit=None, since=None,
                         application_id=None, target_platform=None):
    """Возвращает историю сообщений пользователя.

    target_platform обязателен при просмотре заявки ЧУЖОЙ платформы:
    иначе VK-процесс показывал в TG-заявке переписку VK-пользователя
    с тем же числовым ID.
    """
    conn = None
    rows = []
    pl = target_platform or platform()
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            if application_id is not None:
                if since is not None:
                    base_sql = ("SELECT direction, text, created_at FROM bot_messages "
                                "WHERE platform = %s AND user_id = %s "
                                "AND (application_id = %s "
                                "     OR (application_id IS NULL AND created_at >= %s)) ")
                    base_params = (pl, target_user_id, application_id, since)
                else:
                    base_sql = ("SELECT direction, text, created_at FROM bot_messages "
                                "WHERE platform = %s AND user_id = %s "
                                "AND (application_id = %s OR application_id IS NULL) ")
                    base_params = (pl, target_user_id, application_id)
            elif since is not None:
                base_sql = ("SELECT direction, text, created_at FROM bot_messages "
                            "WHERE platform = %s AND user_id = %s AND created_at >= %s ")
                base_params = (pl, target_user_id, since)
            else:
                base_sql = ("SELECT direction, text, created_at FROM bot_messages "
                            "WHERE platform = %s AND user_id = %s ")
                base_params = (pl, target_user_id)

            if limit is not None:
                cur.execute(base_sql + "ORDER BY created_at DESC LIMIT %s",
                            base_params + (limit,))
                rows = list(reversed(cur.fetchall()))
            else:
                cur.execute(base_sql + "ORDER BY created_at ASC", base_params)
                rows = cur.fetchall()
    except Exception as e:
        logger.error("DB error (history): %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    return rows


# ============================================================
#  ЗАЯВКИ
# ============================================================

def save_application_to_db(data):
    """Сохраняет заявку. Возвращает (ok: bool, app_id: int | None).

    Контракт (ok, app_id) сохранён намеренно:
      - вызывающий код ДОЛЖЕН распаковывать кортеж
        (`success, app_id = save_application_to_db(data)`),
        иначе проверка `if not success` всегда False (кортеж truthy).
      - Раньше функция возвращала bool, но app_id нужен для логов
        и для привязки media_files.

    Особенности:
    - не падает на отсутствии user_id;
    - идемпотентно по client_request_id;
    - сохраняет факт согласия на обработку ПДн (152-ФЗ);
    - после создания заявки привязывает к ней ранее загруженные медиа.
    """
    user_id = data.get("user_id")
    if user_id is None:
        logger.error("save_application_to_db: отсутствует user_id")
        return False, None
    pl = data.get("platform") or platform()
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bright_city_applications
                    (platform, user_id, location, placement_location, user_type, phone, name_age,
                     child_photo_urls, format_choice, drawings_urls, training_info,
                     payment_doc_urls, org_info_attachments, status, created_at,
                     consent_version, consent_text_hash, consent_at, consent_platform,
                     client_request_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'new', NOW(),
                        %s, %s, %s, %s, %s)
                ON CONFLICT (client_request_id) DO NOTHING
                RETURNING id
            """, (
                pl,
                user_id,
                safe_str(data.get("location", "Не указано")),
                safe_str(data.get("placement_location", "Не указано")),
                safe_str(data.get("user_type", "Не указано")),
                safe_str(data.get("phone", "Не указано")),
                safe_str(data.get("name_age", "Не указано")),
                SafeJson(data.get("child_photo_urls", [])),
                safe_str(data.get("format_choice", "Не указано")),
                SafeJson(data.get("drawings_urls", [])),
                safe_str(data.get("training_info", "Не указано")),
                SafeJson(data.get("payment_doc_urls", [])),
                SafeJson(data.get("org_info_attachment_urls", [])),
                data.get("consent_version"),
                data.get("consent_text_hash"),
                data.get("consent_at"),
                pl,
                data.get("client_request_id"),
            ))
            row = cur.fetchone()
        if row is None:
            conn.commit()
            logger.warning("save_application_to_db: дубль по client_request_id — пропущен")
            return True, None
        app_id = row[0]
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE media_files SET application_id = %s
                    WHERE user_platform = %s AND user_id = %s
                      AND application_id IS NULL
                """, (app_id, pl, user_id))
        except Exception as e:
            logger.error("backfill media application_id failed: %s", e)
        conn.commit()
        return True, app_id
    except Exception as e:
        logger.error("DB error (save application): %s", e)
        return False, None
    finally:
        return_db_connection(conn)


# ============================================================
#  ПОИСК ПРЕДЫДУЩИХ ЗАЯВОК
# ============================================================

def has_previous_applications(user_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM bright_city_applications "
                "WHERE platform = %s AND user_id = %s",
                (platform(), user_id))
            return cur.fetchone()[0] > 0
    except Exception as e:
        logger.error("DB error (check applications): %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def get_latest_application(user_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_type, location, org_info_attachments "
                "FROM bright_city_applications "
                "WHERE platform = %s AND user_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (platform(), user_id))
            row = cur.fetchone()
            if row:
                result = {"user_type": row[0], "location": row[1]}
                if row[2]:
                    if isinstance(row[2], list):
                        result["org_info_attachments"] = row[2]
                    elif isinstance(row[2], str):
                        try:
                            result["org_info_attachments"] = json.loads(row[2])
                        except (json.JSONDecodeError, TypeError):
                            result["org_info_attachments"] = []
                    else:
                        result["org_info_attachments"] = []
                else:
                    result["org_info_attachments"] = []
                return result
            return None
    except Exception as e:
        logger.error("DB error (get latest): %s", e)
        return None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


# ============================================================
#  ИЗВЛЕЧЕНИЕ И НОРМАЛИЗАЦИЯ ВЛОЖЕНИЙ
# ============================================================

def extract_phone_from_contact(attachments):
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        if att.get("type") == "contact":
            phone = att["contact"].get("phone")
            if phone:
                return phone.strip()
    return None


def extract_photo_urls(attachments):
    urls = []
    for att in attachments:
        if not isinstance(att, dict) or "type" not in att:
            continue
        if att["type"] == "photo":
            sizes = att.get("photo", {}).get("sizes", [])
            if not sizes:
                continue
            last = sizes[-1]
            url = last.get("url")

            # ФИКС Баг-2: после TG→VK relay поле url = "photo{owner}_{id}"
            # (VK-строка вложения, не URL), а HTTP-адрес CDN переехал в
            # _vk_url (см. TgAdapter._relay_attachments_to_vk).
            #
            # Раньше здесь безусловно возвращался url, и дальнейшие
            # requests.head/get("photo-XXX_YYY") в
            # process_admin_banner_photo / send_banner_to_user падали
            # с MissingSchema: TG-админ не мог отправить макет
            # TG-пользователю.
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                urls.append(url)
            elif last.get("_vk_url"):
                urls.append(last["_vk_url"])
            elif url:
                # Fallback: file_id (TG), если relay не сработал —
                # пусть вызывающий сам решает, что делать.
                urls.append(url)
    return urls


def get_non_photo_types(attachments):
    types = set()
    for att in attachments:
        if not isinstance(att, dict) or "type" not in att:
            continue
        if att["type"] != "photo":
            types.add(att["type"])
    return types


def format_wrong_types(types):
    from core.texts import ATTACHMENT_LABELS
    return ", ".join(ATTACHMENT_LABELS.get(t, t) for t in types)


def normalize_attachments(attachments):
    if not attachments:
        return []
    if isinstance(attachments, str):
        try:
            attachments = json.loads(attachments)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(attachments, list):
        return attachments
    if isinstance(attachments, dict):
        result = []
        for key, value in attachments.items():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        result.append({"type": key, key: item})
                    elif item is not None:
                        result.append({"type": key, key: {"data": item}})
            elif isinstance(value, dict):
                result.append({"type": key, key: value})
            elif value is not None:
                result.append({"type": key, key: {"data": value}})
        return result
    return []


# ============================================================
#  ПАРСИНГ PAYLOAD
# ============================================================

def parse_payload(payload):
    if payload is None:
        return None
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        if len(payload) > MAX_PAYLOAD_LEN:
            logger.warning("parse_payload: слишком длинный payload (%d)", len(payload))
            return None
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return None
        return data if isinstance(data, dict) else None
    return None