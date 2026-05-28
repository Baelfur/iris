"""FastAPI application entrypoint for the PostgreSQL variant."""

from core.app_meta import build_app

from . import db
from .config import settings

app = build_app(
    database="postgresql",
    paramstyle="pyformat",
    settings=settings,
    db_module=db,
    db_user=settings.pg_user,
)
