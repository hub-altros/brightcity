"""JSON API статистики."""
import logging
from fastapi import APIRouter, Depends
from core.db import get_db_connection, return_db_connection
from core.texts import get_status_label
from web_app.dependencies import require_admin
from web_app.api.schemas import StatsResp

logger = logging.getLogger("web.api.stats")
router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("", response_model=StatsResp)
async def get_stats(admin=Depends(require_admin)):
    conn = None
    total = 0
    by_status = []
    by_city = []
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM bright_city_applications")
            total = cur.fetchone()[0]
            cur.execute("SELECT status, COUNT(*) FROM bright_city_applications GROUP BY status")
            by_status = cur.fetchall()
            cur.execute("SELECT location, COUNT(*) FROM bright_city_applications GROUP BY location ORDER BY COUNT(*) DESC LIMIT 10")
            by_city = cur.fetchall()
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    return StatsResp(
        total=total,
        by_status=[{"status": s, "label": get_status_label(s), "count": c} for s, c in by_status],
        by_city=[{"city": c or "—", "count": n} for c, n in by_city],
    )