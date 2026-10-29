"""Download history of the current guest or logged-in account. Plain JSON, needs `Authorization: Bearer`.

The item is stored as the app sends it, so web and mobile can keep their own item shape.
Mobile app entries carry `"source": "app"` (a downloaded file on that phone, no media URLs).
Items are unique per `url`; saving the same URL again updates it and moves it to the top.
"""
import json
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.dependencies.auth import get_current_session
from app.repositories import account_repository as repo


router = APIRouter(prefix="/history")

MAX_ITEM_BYTES = 64 * 1024


class HistoryItemIn(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)
    item: Dict[str, Any]


class HistoryImportIn(BaseModel):
    items: List[HistoryItemIn] = Field(..., max_length=repo.HISTORY_LIMIT)


class HistoryUrlIn(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)


@router.get("")
async def list_history(
    limit: int = Query(default=50, ge=1, le=repo.HISTORY_LIMIT),
    session: dict = Depends(get_current_session),
):
    items = await run_in_threadpool(_run, repo.list_history, session["id"], limit)
    return {"code": 200, "data": {"items": items}, "message": "OK"}


@router.post("")
async def save_history(payload: HistoryItemIn, session: dict = Depends(get_current_session)):
    if len(json.dumps(payload.item)) > MAX_ITEM_BYTES:
        return JSONResponse(status_code=422, content={"code": 422, "data": None, "message": "History item is too large."})
    await run_in_threadpool(_run, repo.save_history, session["id"], payload.url.strip(), payload.item)
    return {"code": 200, "data": None, "message": "Saved to history."}


@router.post("/import")
async def import_history(payload: HistoryImportIn, session: dict = Depends(get_current_session)):
    """Many items at once, oldest first: the app uploads its local history once after updating."""
    if any(len(json.dumps(entry.item)) > MAX_ITEM_BYTES for entry in payload.items):
        return JSONResponse(status_code=422, content={"code": 422, "data": None, "message": "History item is too large."})
    items = [{"url": entry.url.strip(), "item": entry.item} for entry in payload.items]
    added = await run_in_threadpool(_run, repo.import_history, session["id"], items)
    return {"code": 200, "data": {"received": len(items), "added": added}, "message": "History imported."}


@router.post("/delete")
async def delete_history_item(payload: HistoryUrlIn, session: dict = Depends(get_current_session)):
    deleted = await run_in_threadpool(_run, repo.delete_history, session["id"], payload.url.strip())
    return {"code": 200, "data": {"deleted": deleted}, "message": "Removed from history."}


@router.delete("")
async def clear_history(
    scope: str = Query(default="all", pattern="^(all|web)$"),
    session: dict = Depends(get_current_session),
):
    """scope=web (the web app's Clear button) keeps the entries uploaded by the mobile app."""
    deleted = await run_in_threadpool(_run, repo.clear_history, session["id"], scope == "web")
    return {"code": 200, "data": {"deleted": deleted}, "message": "History cleared."}


def _run(function, *args):
    with repo.transaction() as cursor:
        return function(cursor, *args)
