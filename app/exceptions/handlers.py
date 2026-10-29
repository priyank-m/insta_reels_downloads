from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def register_exception_handlers(app: FastAPI) -> None:
    from app.dependencies.auth import NotLoggedIn

    @app.exception_handler(NotLoggedIn)
    async def not_logged_in_handler(request: Request, exc: NotLoggedIn):
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.status_code, "data": exc.data, "message": str(exc)},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        print(f"Unhandled error on {request.method} {request.url.path}: {exc}")
        return JSONResponse(
            status_code=500,
            content={"code": 500, "data": None, "message": str(exc)},
        )

