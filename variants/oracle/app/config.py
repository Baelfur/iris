"""Application configuration loaded from environment variables."""

from core.config.settings import AppSettings


class Settings(AppSettings):
    """Oracle connection and application settings."""

    oracle_host: str
    oracle_port: int = 1521
    oracle_service: str
    oracle_user: str
    oracle_password: str

    @property
    def dsn(self) -> str:
        """Build Oracle DSN string from host, port, and service."""
        return f"{self.oracle_host}:{self.oracle_port}/{self.oracle_service}"


settings = Settings()
