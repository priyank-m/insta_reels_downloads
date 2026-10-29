"""AI plans (Week / Month / Year) through the payment microservice. Plain JSON, `{code, data, message}`.

Public:  GET  /payment/plans
         GET  /payment/status/{token}
         POST /payment/webhook/{key}         called by the payment microservice
Login:   POST /payment/process/{plan_slug}   body: {"email": "..."} only if the account has none
         GET  /payment/latest-status
         GET  /payment/my-subscription
         POST /payment/cancel-subscription
"""
import hmac
import json
from typing import Optional

from fastapi import APIRouter, Body, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.dependencies.auth import NotLoggedIn, get_current_session
from app.services import payment_service


router = APIRouter(prefix="/payment")


async def get_account_session(session: dict = Depends(get_current_session)) -> dict:
    if session["is_guest"]:
        raise NotLoggedIn("Please login to continue.")
    return session


@router.get("/plans")
async def plans():
    return _ok(await run_in_threadpool(payment_service.list_plans), "OK")


@router.post("/process/{plan_slug}")
async def process_payment(
    plan_slug: str,
    payload: Optional[dict] = Body(default=None),
    session: dict = Depends(get_account_session),
):
    email = (payload or {}).get("email")
    try:
        checkout = await run_in_threadpool(
            payment_service.process_payment, session["id"], plan_slug, email if isinstance(email, str) else None
        )
    except payment_service.PaymentError as exc:
        return _error(exc)
    return _ok({"checkout": checkout}, "Payment initiated successfully.")


@router.get("/status/{token}")
async def payment_status(token: str):
    status = await run_in_threadpool(payment_service.payment_status, token)
    if status is None:
        return JSONResponse(status_code=404, content={"code": 404, "data": None, "message": "Transaction not found."})
    return _ok(status, "Payment status fetched successfully.")


@router.get("/latest-status")
async def latest_status(session: dict = Depends(get_account_session)):
    return _ok(await run_in_threadpool(payment_service.latest_payment_status, session["id"]), "OK")


@router.get("/my-subscription")
async def my_subscription(session: dict = Depends(get_account_session)):
    sub = await run_in_threadpool(payment_service.my_subscription, session["id"])
    return _ok(sub, "OK" if sub else "No active plan.")


@router.post("/cancel-subscription")
async def cancel_subscription(session: dict = Depends(get_account_session)):
    try:
        result = await run_in_threadpool(payment_service.cancel_subscription, session["id"])
    except payment_service.PaymentError as exc:
        return _error(exc)
    return _ok(result, "Plan cancelled. You keep access until it expires.")


@router.post("/webhook/{key}")
async def payment_webhook(key: str, request: Request):
    """The microservice posts plain JSON. The secret key in the URL proves it's the microservice."""
    expected = settings.payment_webhook_key
    if not expected or not hmac.compare_digest(key.encode(), expected.encode()):
        print("⚠️ payment webhook rejected: wrong or unconfigured key")
        return JSONResponse(status_code=404, content={"code": 404, "data": None, "message": "Not found."})

    try:
        payload = json.loads(await request.body() or b"{}")
    except ValueError:
        payload = {}
    try:
        outcome = await run_in_threadpool(payment_service.handle_webhook, payload)
        print(f"Payment webhook: {outcome}")
    except payment_service.WebhookNotStored as exc:
        # Nothing saved: answer 5xx so the microservice sends it again.
        print(f"⚠️ payment webhook not stored: {exc}")
        return JSONResponse(status_code=500, content={"code": 500, "data": None, "message": "Try again later."})
    except Exception as exc:
        # The raw event is stored and can be replayed; 200 stops endless retries.
        print(f"⚠️ payment webhook processing error: {type(exc).__name__}: {exc}")
    return {"code": 200, "data": None, "message": "Webhook processed successfully."}


def _ok(data, message: str) -> dict:
    return {"code": 200, "data": data, "message": message}


def _error(exc: payment_service.PaymentError) -> JSONResponse:
    status, body = payment_service.split_error(exc)
    return JSONResponse(status_code=status, content=body)
