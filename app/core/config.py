import os
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import BaseModel


load_dotenv()


class Settings(BaseModel):
    app_name: str = os.getenv("APP_NAME", "Insta Save API")
    app_env: str = os.getenv("APP_ENV", os.getenv("ENVIRONMENT", os.getenv("ENV", "development")))
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))

    db_host: str = os.getenv("DB_HOST", "localhost")
    db_name: str = os.getenv("DB_NAME", "insta_save")
    db_user: str = os.getenv("DB_USER", "root")
    db_password: str = os.getenv("DB_PASSWORD", "password")
    encryption_key: str = os.getenv("APP_ENCRYPTION_KEY", "")
    crypto_allowed_ips: str = os.getenv("CRYPTO_ALLOWED_IPS", "127.0.0.1,::1")

    tor_password: str = os.getenv("TOR_PASSWORD", "qcstup2")
    rotator_log_file: str = os.getenv("ROTATOR_LOG_FILE", "/app/logs/rotator.log")
    rotator_interval_min: int = int(os.getenv("ROTATOR_INTERVAL_MIN", "30"))
    rapidapi_key: str = os.getenv("RAPIDAPI_KEY", "")
    rapidapi_instagram_host: str = os.getenv(
        "RAPIDAPI_INSTAGRAM_HOST",
        "instagram-downloader-download-instagram-videos-stories5.p.rapidapi.com",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
