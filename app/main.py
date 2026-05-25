from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import settings
from app.exceptions.handlers import register_exception_handlers
from app.repositories.settings_repository import seed_env_settings


def create_app() -> FastAPI:
    application = FastAPI(title=settings.app_name)
    application.include_router(api_router)
    register_exception_handlers(application)
    register_startup(application)
    return application


def register_startup(application: FastAPI) -> None:
    @application.on_event("startup")
    def seed_encrypted_settings() -> None:
        try:
            seed_env_settings()
        except Exception as exc:
            print(f"⚠️ Encrypted settings seed skipped: {exc}")


app = create_app()
