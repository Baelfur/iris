"""FastAPI application entrypoint for the MariaDB variant."""

from core.app_meta import build_app

from . import db
from .config import settings

app = build_app(
    database="mariadb",
    paramstyle="pyformat",
    settings=settings,
    db_module=db,
    db_user=settings.mariadb_user,
)
