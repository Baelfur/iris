"""Application configuration loaded from environment variables."""

from core.config.settings import AppSettings


class Settings(AppSettings):
    """Trino connection and application settings."""

    trino_host: str
    trino_port: int = 8080
    trino_user: str
    trino_catalog: str
    trino_scheme: str = "http"

    @property
    def dsn(self) -> str:
        return f"{self.trino_scheme}://{self.trino_host}:{self.trino_port}/{self.trino_catalog}"


settings = Settings()
