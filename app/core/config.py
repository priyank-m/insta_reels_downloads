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
    # Central auth service that verifies Google/Apple sign-in for us.
    auth_service_base_url: str = os.getenv("AUTH_SERVICE_BASE_URL", "")
    auth_service_app: str = os.getenv("AUTH_SERVICE_APP", "")
    auth_service_timeout_seconds: float = float(os.getenv("AUTH_SERVICE_TIMEOUT_SECONDS", "15"))
    # v2 API encryption (RNCryptor). The password is key[offset:offset + length].
    auth_service_rncryptor_key: str = os.getenv("AUTH_SERVICE_RNCRYPTOR_KEY", "")
    auth_service_rncryptor_offset: int = int(os.getenv("AUTH_SERVICE_RNCRYPTOR_OFFSET", "8"))
    auth_service_rncryptor_length: int = int(os.getenv("AUTH_SERVICE_RNCRYPTOR_LENGTH", "32"))
    # AES-256-GCM key shared with the Flutter app for the encrypted /auth endpoints.
    client_encryption_key: str = os.getenv("CLIENT_ENCRYPTION_KEY", "")

    # Payments go through the payment microservice, which holds the Razorpay secret.
    payment_url: str = os.getenv("PAYMENT_URL", "")
    payment_project_code: str = os.getenv("PAYMENT_PROJECT_CODE", "")
    payment_project_slug: str = os.getenv("PAYMENT_PROJECT_SLUG", "")
    payment_timeout_seconds: float = float(os.getenv("PAYMENT_TIMEOUT_SECONDS", "20"))
    # Public Razorpay key id: sent to the browser to open checkout.js. Never the secret.
    razorpay_key_id: str = os.getenv("RAZORPAY_KEY_ID", "")
    # Random value in the webhook URL given to the microservice: /payment/webhook/{key}.
    payment_webhook_key: str = os.getenv("PAYMENT_WEBHOOK_KEY", "")
    # Comma-separated account emails charged by the "recurring-test" plan (₹1) instead.
    test_payment_user_emails: str = os.getenv("TEST_PAYMENT_USER_EMAILS", "")
    payment_checkout_name: str = os.getenv("PAYMENT_CHECKOUT_NAME", "InstaSaver AI")
    # When true, AI endpoints need an active plan (send the session token). Off keeps old apps working.
    ai_requires_subscription: bool = os.getenv("AI_REQUIRES_SUBSCRIPTION", "false").lower() in {"1", "true", "yes", "on"}

    # Old APIs (/download_media, /frontend_success and the six AI tools):
    # 1 = only AES-GCM encrypted requests (CLIENT_ENCRYPTION_KEY); 0 = encrypted or plain both work.
    encryption_required: bool = os.getenv("ENCRYPTION_REQUIRED", "0").strip().lower() in {"1", "true", "yes", "on"}

    instagram_oembed_metadata_enrichment_enabled: bool = (
        os.getenv("INSTAGRAM_OEMBED_METADATA_ENRICHMENT_ENABLED", "false").lower()
        in {"1", "true", "yes", "on"}
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
