from fastapi import APIRouter, Depends, HTTPException

from app.core.crypto import decrypt_secret, encrypt_secret
from app.dependencies.ip_allowlist import require_crypto_allowed_ip
from app.schemas.common import ApiResponse
from app.schemas.crypto import CryptoRequest


router = APIRouter(
    prefix="/crypto",
    dependencies=[Depends(require_crypto_allowed_ip)],
)


@router.post("/encrypt", response_model=ApiResponse)
async def encrypt_value(payload: CryptoRequest):
    try:
        return {
            "code": 200,
            "data": {"value": encrypt_secret(payload.value)},
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/decrypt", response_model=ApiResponse)
async def decrypt_value(payload: CryptoRequest):
    try:
        return {
            "code": 200,
            "data": {"value": decrypt_secret(payload.value)},
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

