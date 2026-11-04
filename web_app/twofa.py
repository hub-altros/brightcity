"""TOTP-2FA для веб-админок."""
import hashlib
import io
import logging
import secrets
import time

import pyotp
import qrcode

from web_app.db_helpers import (
    update_totp_secret, confirm_totp, set_totp_last_step,
    add_recovery_codes, consume_recovery_code,
)

logger = logging.getLogger("web.2fa")

STEP_SECONDS = 30
VALID_WINDOW = 1


def _make_qr_png(otpauth_url: str) -> bytes:
    img = qrcode.make(otpauth_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def setup_totp(admin_id: int, login: str) -> dict:
    """Генерирует новый секрет (не активирует). Возвращает secret + QR PNG."""
    secret = pyotp.random_base32()
    otpauth = pyotp.TOTP(secret).provisioning_uri(
        name=login, issuer_name="Яркий!Город — Админка")
    update_totp_secret(admin_id, secret)
    return {"secret": secret, "otpauth_url": otpauth,
            "qr_png": _make_qr_png(otpauth)}


def _hash_recovery_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _format_recovery_code(raw: str) -> str:
    raw = raw.upper()
    return f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}"


def confirm_totp_setup(admin_id: int, secret: str, code: str) -> tuple[bool, list[str]]:
    totp = pyotp.TOTP(secret)
    if not totp.verify(code, valid_window=VALID_WINDOW):
        return False, []
    recovery_codes = [_format_recovery_code(secrets.token_hex(5))
                      for _ in range(10)]
    add_recovery_codes(admin_id, [_hash_recovery_code(c)
                                  for c in recovery_codes])
    confirm_totp(admin_id)
    return True, recovery_codes


def verify_totp(admin_id: int, secret: str, code: str,
                last_step: int | None) -> bool:
    code = (code or "").strip()
    if not code or not secret:
        return False
    totp = pyotp.TOTP(secret)
    now_step = int(time.time()) // STEP_SECONDS
    matched = None
    for offset in range(-VALID_WINDOW, VALID_WINDOW + 1):
        step = now_step + offset
        try:
            expected = totp.at(step * STEP_SECONDS)
        except Exception:
            continue
        if secrets.compare_digest(expected, code):
            matched = step
            break
    if matched is None:
        return False
    if last_step is not None and matched <= last_step:
        return False
    set_totp_last_step(admin_id, matched)
    return True


def verify_recovery(admin_id: int, code: str) -> bool:
    code = (code or "").strip().upper().replace(" ", "")
    if not code:
        return False
    return consume_recovery_code(admin_id, _hash_recovery_code(code))