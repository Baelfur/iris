"""FastAPI application entrypoint for the MySQL variant."""

from core.app_meta import build_app

from . import db
from .config import settings

app = build_app(
    database="mysql",
    paramstyle="pyformat",
    settings=settings,
    db_module=db,
    db_user=settings.mysql_user,
)
