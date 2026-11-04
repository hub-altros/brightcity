"""Пароли и подпись сессий."""
"""Пароли и подпись сессий."""
import hashlib
import logging
import os
import secrets

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

logger = logging.getLogger("web.auth")

# ФИКС: единый секрет для itsdangerous и SessionMiddleware.
# Вычисляется ОДИН раз при импорте — main.py читает его отсюда,
# а не генерирует свой. Иначе сессии, подписанные одним ключом,
# не проверяются другим (2FA слетает после рестарта).
_WEB_SECRET = os.getenv("WEB_SECRET_KEY")
if not _WEB_SECRET:
    _WEB_SECRET = os.urandom(32).hex()
    logger.warning(
        "WEB_SECRET_KEY не задан в .env — сгенерирован разовый ключ. "
        "Сессии будут слетать после рестарта. "
        "Добавьте WEB_SECRET_KEY=<случайные 32+ символа> в .env.")

WEB_SECRET = _WEB_SECRET  # экспорт для web_app/main.py

_serializer = None


def _get_serializer():
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(WEB_SECRET)
    return _serializer


# --- Пароли ---

def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(),
                                 200_000).hex()
    return f"pbkdf2$200000${salt}${digest}"


def verify_password(pw: str, hashed: str) -> bool:
    try:
        algo, iters, salt, digest = hashed.split("$")
        if algo != "pbkdf2":
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256", pw.encode(), salt.encode(), int(iters)).hex()
        return secrets.compare_digest(expected, digest)
    except Exception:
        return False


# --- Кука сессии ---

SESSION_COOKIE = "yg_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 дней


def sign_session(session_id: str) -> str:
    return _get_serializer().dumps(session_id)


def unsign_session(cookie: str) -> str | None:
    if not cookie:
        return None
    try:
        return _get_serializer().loads(cookie, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None