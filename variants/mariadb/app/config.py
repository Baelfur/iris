"""Application configuration loaded from environment variables."""

from core.config.settings import AppSettings


class Settings(AppSettings):
    """MariaDB connection and application settings."""

    mariadb_host: str
    mariadb_port: int = 3306
    mariadb_user: str
    mariadb_password: str
    mariadb_database: str

    @property
    def dsn(self) -> str:
        return f"{self.mariadb_host}:{self.mariadb_port}/{self.mariadb_database}"


settings = Settings()
