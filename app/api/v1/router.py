from fastapi import APIRouter

from app.api.v1.endpoints.auth import router as auth_router
from app.api.v1.endpoints.crypto import router as crypto_router
from app.api.v1.endpoints.health import router as health_router
from app.api.v1.endpoints.history import router as history_router
from app.api.v1.endpoints.instagram import router as instagram_router
from app.api.v1.endpoints.payments import router as payments_router


api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(history_router)
api_router.include_router(payments_router)
api_router.include_router(crypto_router)
api_router.include_router(instagram_router)
