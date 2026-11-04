"""Фильтры для UI: города, локации, статусы."""
import logging

from fastapi import APIRouter, Depends

from core.db import get_db_connection, return_db_connection
from web_app.dependencies import require_api_admin
from web_app.api.schemas import FiltersResp

logger = logging.getLogger("web.api.filters")
router = APIRouter(prefix="/api/filters", tags=["filters"])

_STATUSES = [
    {"value": "",             "label": "Все"},
    {"value": "new",          "label": "Новые"},
    {"value": "in_progress",  "label": "В работе"},
    {"value": "rejected",     "label": "Отклонены"},
    {"value": "done",         "label": "Завершены"},
]


@router.get("/applications", response_model=FiltersResp)
async def application_filters(admin=Depends(require_api_admin)):
    conn = None
    cities = []
    placement_locations = []
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT location, COUNT(*) FROM bright_city_applications
                WHERE location IS NOT NULL AND location != ''
                GROUP BY location ORDER BY location
            """)
            cities = [{"value": r[0], "label": r[0], "count": r[1]}
                      for r in cur.fetchall()]
            cur.execute("""
                SELECT placement_location, COUNT(*)
                FROM bright_city_applications
                WHERE placement_location IS NOT NULL
                  AND placement_location != ''
                GROUP BY placement_location ORDER BY placement_location
            """)
            placement_locations = [
                {"value": r[0], "label": r[0], "count": r[1]}
                for r in cur.fetchall()
            ]
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    return FiltersResp(
        cities=cities,
        placement_locations=placement_locations,
        statuses=_STATUSES,
    )