"""Application configuration loaded from environment variables."""

from core.config.settings import AppSettings


class Settings(AppSettings):
    """PostgreSQL connection and application settings."""

    pg_host: str
    pg_port: int = 5432
    pg_user: str
    pg_password: str
    pg_database: str

    @property
    def dsn(self) -> str:
        return f"{self.pg_host}:{self.pg_port}/{self.pg_database}"


settings = Settings()
