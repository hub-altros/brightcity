"""JSON API для GDPR-запросов."""
import logging
from fastapi import APIRouter, Depends, HTTPException

from core.db import (
    list_pending_gdpr_requests, get_gdpr_request_by_id, log_admin_action,
)
from core.gdpr import delete_user_data, anonymize_user_data
from web_app.dependencies import require_admin, run_blocking
from web_app.api.schemas import GdprItem, OkResp

logger = logging.getLogger("web.api.gdpr")
router = APIRouter(prefix="/api/gdpr", tags=["gdpr"])


@router.get("", response_model=list[GdprItem])
async def list_gdpr(admin=Depends(require_admin)):
    rows = list_pending_gdpr_requests(limit=200)
    return [GdprItem(id=r[0], platform=r[1], user_id=r[2], requested_at=r[3])
            for r in rows]


@router.post("/{request_id}/delete", response_model=OkResp)
async def do_delete(request_id: int, admin=Depends(require_admin)):
    row = get_gdpr_request_by_id(request_id)
    if not row:
        raise HTTPException(404)
    tp, tuid = row
    log_admin_action(admin[0], "gdpr_delete",
                     target_platform=tp, target_id=tuid, params={"source": "web"})
    try:
        await run_blocking(delete_user_data, tp, tuid,
                           admin_id=admin[0], admin_platform="web", notify_user=True)
    except Exception as e:
        logger.exception("gdpr delete failed: %s", e)
        raise HTTPException(500, str(e))
    return OkResp()


@router.post("/{request_id}/anon", response_model=OkResp)
async def do_anon(request_id: int, admin=Depends(require_admin)):
    row = get_gdpr_request_by_id(request_id)
    if not row:
        raise HTTPException(404)
    tp, tuid = row
    log_admin_action(admin[0], "gdpr_anonymize",
                     target_platform=tp, target_id=tuid, params={"source": "web"})
    try:
        await run_blocking(anonymize_user_data, tp, tuid,
                           admin_id=admin[0], admin_platform="web", notify_user=True)
    except Exception as e:
        logger.exception("gdpr anon failed: %s", e)
        raise HTTPException(500, str(e))
    return OkResp()