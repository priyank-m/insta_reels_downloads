from fastapi import HTTPException, Request

from app.core.config import settings


def require_crypto_allowed_ip(request: Request) -> None:
    allowed_ips = {
        ip.strip()
        for ip in settings.crypto_allowed_ips.split(",")
        if ip.strip()
    }

    client_ip = request.client.host if request.client else ""
    forwarded_for = request.headers.get("x-forwarded-for", "")
    forwarded_ip = forwarded_for.split(",", 1)[0].strip() if forwarded_for else ""

    if client_ip in allowed_ips or forwarded_ip in allowed_ips:
        return

    raise HTTPException(status_code=403, detail="IP not allowed")
