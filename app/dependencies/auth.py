from typing import Any, Dict, Optional

from fastapi import Header
from fastapi.concurrency import run_in_threadpool

from app.core.config import settings
from app.services import payment_service, session_service


class NotLoggedIn(Exception):
    """Answered as `{code, data, message}` with `status_code` (see app.exceptions.handlers)."""

    def __init__(self, message: str = "Unauthorized", status_code: int = 401, data: Optional[dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.data = data


async def get_current_session(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    """Require `Authorization: Bearer <token>` from guest login or Google/Apple login.

    Returns the session joined with its user: `id` (user id), `is_guest`, `session_id`, ...
    Usage: `async def endpoint(session: dict = Depends(get_current_session)): ...`
    """
    token = session_service.bearer_token(authorization)
    if not token:
        raise NotLoggedIn()
    try:
        return await run_in_threadpool(session_service.session_for_token, token)
    except session_service.SessionError as exc:
        raise NotLoggedIn(str(exc))
    except Exception as exc:
        print(f"⚠️ session check failed: {type(exc).__name__}: {exc}")
        raise NotLoggedIn("Something went wrong. Please try again.", status_code=500)


async def require_ai_access(authorization: Optional[str] = Header(default=None)) -> None:
    """AI endpoints: when AI_REQUIRES_SUBSCRIPTION is on, need a logged-in account with an active plan.

    Off by default, so apps that don't send a session token yet keep working.
    """
    if not settings.ai_requires_subscription:
        return
    session = await get_current_session(authorization)
    if session["is_guest"]:
        raise NotLoggedIn("Please login to use the AI tools.", data={"requires_login": True})
    try:
        active = await run_in_threadpool(payment_service.has_active_subscription, session["id"])
    except Exception as exc:
        print(f"⚠️ plan check failed: {type(exc).__name__}: {exc}")
        raise NotLoggedIn("Something went wrong. Please try again.", status_code=500)
    if not active:
        raise NotLoggedIn("An active plan is required to use the AI tools.", status_code=402,
                          data={"requires_subscription": True})
