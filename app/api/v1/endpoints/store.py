"""App store purchases (App Store / Google Play) bound to the logged-in account or the guest.
Encrypted both ways (app.middleware.legacy_encryption.is_new_api), needs `Authorization: Bearer`.

POST /store/verify        {"platform": "apple", "receipt": "<base64 app receipt>"}
                          {"platform": "google", "purchase_token": "..."}
GET  /store/entitlements  -> {premium, plus, sources: [...]}
Both answer the user's entitlements; `owned_by_other_account: true` means a purchase on this
store account belongs to another of our accounts (log in with that one to use it).
"""
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.dependencies.auth import get_current_session
from app.services import store_service
from app.services import store_verification as stores


router = APIRouter(prefix="/store")

OTHER_ACCOUNT_MESSAGE = "This purchase belongs to another account. Log in with that account to use it."


class VerifyIn(BaseModel):
    platform: str = Field(..., pattern="^(apple|google)$")
    receipt: Optional[str] = Field(default=None, max_length=1_000_000)
    purchase_token: Optional[str] = Field(default=None, max_length=4096)


@router.post("/verify")
async def verify(payload: VerifyIn, session: dict = Depends(get_current_session)):
    try:
        data = await run_in_threadpool(
            store_service.verify, session["id"], payload.platform, payload.receipt, payload.purchase_token
        )
    except ValueError as exc:  # missing receipt / token
        return _error(422, str(exc))
    except stores.InvalidPurchase as exc:
        print(f"⚠️ store verify rejected: {exc}")
        return _error(422, "This purchase could not be verified.")
    except stores.StoreUnavailable as exc:
        print(f"⚠️ store verify unavailable: {exc}")
        return _error(503, "Purchase check is not available right now. Please try again later.")
    message = OTHER_ACCOUNT_MESSAGE if data["owned_by_other_account"] else "OK"
    return {"code": 200, "data": data, "message": message}


@router.get("/entitlements")
async def entitlements(session: dict = Depends(get_current_session)):
    data = await run_in_threadpool(store_service.entitlements, session["id"])
    return {"code": 200, "data": data, "message": "OK"}


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": status, "data": None, "message": message})
