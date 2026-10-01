from fastapi import APIRouter

from app.api.v1.endpoints.crypto import router as crypto_router
from app.api.v1.endpoints.health import router as health_router
from app.api.v1.endpoints.instagram import router as instagram_router


api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(crypto_router)
api_router.include_router(instagram_router)
