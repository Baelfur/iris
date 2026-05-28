"""Application configuration loaded from environment variables."""

from core.config.settings import AppSettings


class Settings(AppSettings):
    """MySQL connection and application settings."""

    mysql_host: str
    mysql_port: int = 3306
    mysql_user: str
    mysql_password: str
    mysql_database: str

    @property
    def dsn(self) -> str:
        return f"{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"


settings = Settings()
