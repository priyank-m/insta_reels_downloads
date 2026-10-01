from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.services.health_service import readiness_payload


router = APIRouter(prefix="/api")


@router.get("/health")
async def health() -> JSONResponse:
    payload = readiness_payload()
    return JSONResponse(status_code=200 if payload["status"] else 503, content=payload)
