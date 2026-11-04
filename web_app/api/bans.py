"""Управление банами: список, бан, разбан."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from core.db import (
    get_banned_users_list, count_banned_users, log_admin_action,
)
from core.users import ban_user, unban_user, is_admin_user
from core.notify import send_message
from web_app.dependencies import require_api_admin
from web_app.api.schemas import BanItem, BanListResp, BanRequest, OkResp

logger = logging.getLogger("web.api.bans")
router = APIRouter(prefix="/api/bans", tags=["bans"])

_VALID_PLATFORMS = ("vk", "tg", "max")


@router.get("", response_model=BanListResp)
async def list_bans(
    platform: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    admin=Depends(require_api_admin),
):
    """Список активных банов. platform=None — все платформы."""
    if platform is not None and platform not in _VALID_PLATFORMS:
        raise HTTPException(400, "platform: vk|tg|max или None")

    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    rows = get_banned_users_list(
        platform_name=platform, limit=limit, offset=offset)
    total = count_banned_users(platform_name=platform)

    items = []
    for r in rows:
        (pl, uid, banned_by, banned_by_pl,
         banned_at, expires_at, reason, ban_count) = r
        items.append(BanItem(
            platform=pl, user_id=uid,
            banned_by=banned_by, banned_by_platform=banned_by_pl,
            banned_at=banned_at, expires_at=expires_at,
            reason=reason, ban_count=ban_count or 0,
        ))
    return BanListResp(items=items, total=total, limit=limit, offset=offset)


@router.post("", response_model=OkResp)
async def create_ban(body: BanRequest, admin=Depends(require_api_admin)):
    """Забанить пользователя. Идемпотентно."""
    if body.user_id <= 0:
        raise HTTPException(400, "user_id должен быть > 0")
    if is_admin_user(body.user_id, target_platform=body.platform):
        raise HTTPException(400, "Нельзя заблокировать администратора")

    reason = (body.reason or "").strip()[:2000]
    if not ban_user(body.user_id, admin[0], reason,
                    target_platform=body.platform):
        raise HTTPException(500, "Не удалось заблокировать")

    log_admin_action(
        admin[0], "ban", target_id=body.user_id,
        params={"reason": reason, "platform": body.platform, "source": "web"},
    )
    try:
        send_message(body.user_id, "🚫 Вы заблокированы администратором.",
                     target_platform=body.platform)
    except Exception:
        pass
    return OkResp()


@router.post("/{platform}/{user_id}/unban", response_model=OkResp)
async def unban_user_route(
    platform: str, user_id: int, admin=Depends(require_api_admin),
):
    if platform not in _VALID_PLATFORMS:
        raise HTTPException(400, "platform: vk|tg|max")
    if user_id <= 0:
        raise HTTPException(400, "user_id должен быть > 0")
    if not unban_user(user_id, target_platform=platform):
        raise HTTPException(500, "Не удалось разблокировать")
    log_admin_action(
        admin[0], "unban", target_id=user_id,
        params={"platform": platform, "source": "web"},
    )
    return OkResp()