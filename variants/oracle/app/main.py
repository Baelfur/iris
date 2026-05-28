"""FastAPI application entrypoint for the Oracle variant."""

from core.app_meta import build_app

from . import db
from .config import settings

app = build_app(
    database="oracle",
    paramstyle="named",
    settings=settings,
    db_module=db,
    db_user=settings.oracle_user,
)
