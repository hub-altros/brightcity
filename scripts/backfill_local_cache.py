"""Одноразовый backfill: для всех media_files без локального кэша
скачивает файл (VK CDN / VK-строка / TG file_id / web://-баннер) и
кладёт в web_app/media_cache/{id}. vk_url заменяется на постоянный URL.

Запуск:
    python scripts/backfill_local_cache.py
    python scripts/backfill_local_cache.py --from-id 65
    python scripts/backfill_local_cache.py --no-head

Требует в .env:
  - VK_USER_TOKEN  — ОБЯЗАТЕЛЬНО для photos.getById (community token
                     даёт [27]); https://vkhost.github.io/ (photos, docs)
  - VK_RELAY_TOKEN / VK_BOT_TOKEN — для скачивания CDN
  - TG_BOT_TOKEN   — для TG file_id
  - работающий run_web.py (для sign_media_url)

Особенности:
  * _safe_download: своя сессия без retries, stream=True,
    timeout=(5,10) + wall-clock 20 сек. Мёртвый URL не блокирует
    очередь: упадёт через 20 сек максимум, не через 120 (как
    _get_vk_http_session с Retry(total=3, read=3)).
  * _quick_head_check: перед скачиванием — HEAD-запрос 3 сек.
    Если HEAD не отвечает — пропускаем без попытки GET (экономит
    17 сек на каждый мёртвый URL). Отключается флагом --no-head.
  * Fallback: если url — мёртвый CDN, но в БД есть
    vk_owner_id+vk_photo_id — photos.getById даёт свежий URL.
  * web://banner/* — баннеры из веб-админки, файлы уже в
    media_cache, просто обновляем vk_url.
  * --from-id N — начать с media_files.id >= N (для продолжения
    после сбоя/зависания).
"""
import os
import re
import sys
import time
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from dotenv import load_dotenv
load_dotenv(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
import core.media as _media
from vk_api import VkApi
from core.db import (
    get_db_connection, return_db_connection,
    update_media_file_local_url,
)

# Жёсткий wall-clock лимит на одно скачивание. Не даём backfill'у
# висеть на мёртвых VK CDN URL: requests с retry policy может
# ждать до 120 секунд на один URL.
_DOWNLOAD_WALL_TIMEOUT = 20  # секунд
_DOWNLOAD_MAX_SIZE = 50 * 1024 * 1024  # 50 МБ

# HEAD-проверка: если URL не отвечает за 3 секунды — считаем мёртвым
# и не тратим 20 секунд на GET.
_HEAD_TIMEOUT = 3
_HEAD_ENABLED = True  # управляется флагом --no-head


def _is_local_url(u):
    if not isinstance(u, str):
        return False
    return "/media/raw/" in u or u.startswith("web://")


def _quick_head_check(url: str) -> bool:
    """Быстрая проверка, что URL жив (HEAD, 3 сек).

    Возвращает True, если HEAD вернул < 400. Многие CDN не поддерживают
    HEAD и отвечают 405 — тогда тоже возвращаем True (пробуем GET),
    потому что 405 = «сервер жив, метод не разрешён», а не «URL мёртв».
    """
    if not _HEAD_ENABLED:
        return True
    if not url or not isinstance(url, str):
        return False
    if not url.startswith(("http://", "https://")):
        return False
    try:
        r = requests.head(url, timeout=_HEAD_TIMEOUT,
                          allow_redirects=True, verify=False)
        # 405 Method Not Allowed — CDN не любит HEAD, но URL рабочий
        if r.status_code == 405:
            return True
        return r.status_code < 400
    except Exception:
        return False


def _safe_download(url: str) -> "bytes | None":
    """Скачивание с wall-clock таймаутом (Windows-safe).

    Своя сессия без retries — хотим упасть быстро, а не ретраить
    мёртвый URL 3 раза по 30 секунд. stream=True — не держим
    всё в памяти, если сервер отдаёт гигабайты.
    """
    if not url or not isinstance(url, str):
        return None
    if not url.startswith(("http://", "https://")):
        return None

    def _fetch():
        s = requests.Session()
        s.trust_env = False
        s.proxies = {"http": None, "https": None}
        try:
            resp = s.get(url, timeout=(5, 10), stream=True,
                         allow_redirects=True, verify=False)
            if resp.status_code != 200:
                print(f"    download status={resp.status_code} ({url[:50]})")
                return None
            buf = bytearray()
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > _DOWNLOAD_MAX_SIZE:
                    print(f"    download too big ({len(buf)} bytes)")
                    return None
            return bytes(buf) if buf else None
        except Exception as e:
            print(f"    download error: {type(e).__name__}: {e}")
            return None
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=1) as ex:
        try:
            return ex.submit(_fetch).result(timeout=_DOWNLOAD_WALL_TIMEOUT)
        except FutTimeout:
            print(f"    download TIMEOUT (>{_DOWNLOAD_WALL_TIMEOUT}s): "
                  f"{url[:60]}")
            return None


def _make_user_api():
    """VK API с user token — photos.getById / docs.getById.
    Community token даёт [27] Group authorization failed.
    """
    token = (os.getenv("VK_USER_TOKEN") or "").strip()
    if not token or token.startswith("fake-"):
        return None
    try:
        return VkApi(
            token=token,
            session=_media._get_vk_http_session(),
        ).get_api()
    except Exception as e:
        print(f"  WARNING: VK_USER_TOKEN invalid: {e}")
        return None


def _resolve_cdn_url(user_api, url):
    """VK-строка 'photo-X_Y' / 'doc-X_Y' → свежий CDN URL."""
    if not url.startswith(("photo", "doc")) or "_" not in url:
        return None
    m = re.match(r"^(photo|doc)(-?\d+)_(\d+)$", url)
    if not m:
        return None
    kind, owner_s, item_s = m.groups()
    oid, iid = int(owner_s), int(item_s)
    try:
        if kind == "photo":
            photos = user_api.photos.getById(photos=f"{oid}_{iid}")
            if not photos:
                return None
            sizes = photos[0].get("sizes") or []
            if not sizes:
                return None
            mx = max(
                sizes,
                key=lambda s: (s.get("width") or 0) * (s.get("height") or 0),
            )
            return mx.get("url")
        else:
            docs = user_api.docs.getById(docs=f"{oid}_{iid}")
            if not docs:
                return None
            return docs[0].get("url")
    except Exception as e:
        print(f"    VK API failed for {url}: {e}")
        return None


def _resolve_cdn_by_ids(user_api, owner_id, photo_id, doc_id):
    """По vk_owner_id + vk_photo_id/vk_doc_id — свежий CDN URL."""
    if user_api is None or owner_id is None:
        return None
    try:
        if photo_id is not None:
            photos = user_api.photos.getById(
                photos=f"{owner_id}_{photo_id}")
            if not photos:
                return None
            sizes = photos[0].get("sizes") or []
            if not sizes:
                return None
            mx = max(
                sizes,
                key=lambda s: (s.get("width") or 0) * (s.get("height") or 0),
            )
            return mx.get("url")
        if doc_id is not None:
            docs = user_api.docs.getById(
                docs=f"{owner_id}_{doc_id}")
            if not docs:
                return None
            return docs[0].get("url")
    except Exception as e:
        print(f"    VK API failed for {owner_id}_{photo_id or doc_id}: {e}")
    return None


def _download_tg_file_id(file_id):
    """TG file_id → bytes. Сбрасывает _tg_bot, чтобы httpx-клиент
    пересоздался в новом event loop (иначе 'Event loop is closed')."""
    try:
        import web_app.media as _wm
        _wm._tg_bot = None
        bot = _wm._get_tg_bot()
        if bot is None:
            return None

        async def _fetch():
            import io
            f = await bot.get_file(file_id=file_id)
            buf = io.BytesIO()
            await f.download_to_memory(buf)
            return buf.getvalue()

        try:
            asyncio.get_running_loop()
            running = True
        except RuntimeError:
            running = False

        if running:
            with ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(lambda: asyncio.run(_fetch())).result(
                    timeout=30)
        return asyncio.run(_fetch())
    except Exception as e:
        print(f"    TG getFile failed for {file_id[:24]}…: {e}")
        return None


def _try_download_with_head(url: str, label: str) -> "bytes | None":
    """HEAD-проверка + скачивание. Экономит 17 сек на мёртвых URL."""
    if _HEAD_ENABLED:
        if not _quick_head_check(url):
            print(f"    {label}: HEAD failed, skip ({url[:60]})")
            return None
    return _safe_download(url)


def main():
    global _HEAD_ENABLED

    ap = argparse.ArgumentParser()
    ap.add_argument("--from-id", type=int, default=0,
                    help="Начать с media_files.id >= N")
    ap.add_argument("--no-head", action="store_true",
                    help="Отключить HEAD-проверку (медленнее, но "
                         "надёжнее для CDN с нестандартным HEAD)")
    args = ap.parse_args()

    if args.no_head:
        _HEAD_ENABLED = False

    _media._ensure_vk_uploader()
    user_api = _make_user_api()
    if user_api is None:
        print(
            "WARNING: VK_USER_TOKEN не задан — VK-строки "
            "(photo-X_Y / doc-X_Y) и резолв по vk_photo_id "
            "НЕ будут обработаны.\n"
            "         Получите user token на https://vkhost.github.io/\n"
            "         (права: photos, docs) и добавьте в .env как VK_USER_TOKEN.\n"
        )

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, url, vk_url,
                       source_file_id, source_platform, user_platform,
                       vk_owner_id, vk_photo_id, vk_doc_id
                FROM media_files
                WHERE (vk_url IS NULL
                       OR vk_url LIKE 'https://%%vkuserphoto.ru/%%'
                       OR vk_url LIKE 'https://%%userapi.com/%%'
                       OR url LIKE 'web://%%')
                  AND id >= %s
                ORDER BY id
            """, (args.from_id,))
            rows = cur.fetchall()
        print(f"found {len(rows)} rows without local cache")
        if _HEAD_ENABLED:
            print(f"HEAD-check: enabled (timeout {_HEAD_TIMEOUT}s)")
        else:
            print("HEAD-check: disabled")

        from web_app.media import (
            save_media_bytes_to_cache, sign_media_url, _cache_path,
        )

        updated = skipped = failed = 0
        t_start = time.time()

        for row in rows:
            (mid, url, vk_url, src_id, src_pl, user_pl,
             vk_oid, vk_pid, vk_did) = row

            if _is_local_url(vk_url) and not url.startswith("web://"):
                continue

            print(f"  [{mid}] url={url[:45]}…")

            # web:// — баннеры из веб-админки
            if url.startswith("web://"):
                path = _cache_path(mid)
                if os.path.isfile(path) and os.path.getsize(path) > 0:
                    local_url = sign_media_url(mid)
                    update_media_file_local_url(mid, local_url)
                    updated += 1
                    print(f"    [{mid}] web:// → файл на диске, vk_url обновлён")
                else:
                    skipped += 1
                    print(f"    [{mid}] SKIP: web://banner, файла нет")
                continue

            data = None
            via = ""

            # 1) TG file_id в url
            if url.startswith("AgAC"):
                data = _download_tg_file_id(url)
                via = "tg_file_id"
            elif src_id and src_pl == "tg":
                data = _download_tg_file_id(src_id)
                via = "source_file_id(tg)"

            # 2) vk_url = живой CDN (сейчас редко)
            if data is None and isinstance(vk_url, str) and vk_url.startswith("http"):
                data = _try_download_with_head(vk_url, "vk_url")
                if data:
                    via = "vk_url_cdn"

            # 3) url = VK CDN (старые записи) — HEAD + download
            if data is None and url.startswith("http"):
                data = _try_download_with_head(url, "url_http")
                if data:
                    via = "url_http"

            # 4) url = VK-строка → photos.getById → download
            if data is None and url.startswith(("photo", "doc")):
                if user_api is None:
                    skipped += 1
                    print(f"    [{mid}] SKIP: нет VK_USER_TOKEN")
                    continue
                cdn = _resolve_cdn_url(user_api, url)
                if cdn:
                    data = _safe_download(cdn)  # HEAD уже прошёл, VK CDN живой
                    if data:
                        via = "vk_str_cdn"

            # 5) url — мёртвый CDN, но в БД есть vk_owner_id+photo_id
            if (data is None and user_api is not None
                    and vk_oid is not None
                    and (vk_pid is not None or vk_did is not None)):
                print(f"    [{mid}] fallback: vk_owner_id={vk_oid} "
                      f"photo_id={vk_pid} doc_id={vk_did}")
                cdn = _resolve_cdn_by_ids(user_api, vk_oid, vk_pid, vk_did)
                if cdn:
                    data = _safe_download(cdn)  # свежий, HEAD не нужен
                    if data:
                        via = "fallback_by_ids"

            if not data:
                failed += 1
                print(f"    [{mid}] FAILED — all sources exhausted")
                continue

            save_media_bytes_to_cache(mid, data)
            local_url = sign_media_url(mid)
            update_media_file_local_url(mid, local_url)
            updated += 1
            print(f"    [{mid}] OK via {via} ({len(data)} bytes) → cached")

        print(
            f"\nupdated {updated}/{len(rows)}  "
            f"(skipped={skipped}, failed={failed})  "
            f"in {time.time() - t_start:.1f}s"
        )
    finally:
        conn.rollback()
        return_db_connection(conn)


if __name__ == "__main__":
    main()