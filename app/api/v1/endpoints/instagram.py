from typing import Optional

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import conint

from app.services import instagram_service


router = APIRouter()


@router.post("/download_media")
async def download_media(
    instagramURL: str = Form(...),
    deviceId: str = Form(min_length=1),
    deviceType: Optional[conint(ge=1, le=2)] = Form(default=None),
):
    return await instagram_service.download_media(
        instagramURL=instagramURL,
        deviceId=deviceId,
        deviceType=deviceType,
    )


@router.post("/frontend_success")
async def frontend_success(
    deviceId: str = Form(...),
    deviceType: Optional[conint(ge=1, le=2)] = Form(default=None),
):
    return await instagram_service.frontend_success(deviceId=deviceId, deviceType=deviceType)


@router.post("/trendy_captions")
async def trendy_captions(
    video_file: UploadFile = File(...),
    caption: str = Form(default=""),
    niche: str = Form(default=""),
):
    return await instagram_service.trendy_captions(
        video_file=video_file,
        caption=caption,
        niche=niche,
    )


@router.post("/trendy_hashtags")
async def trendy_hashtags(
    video_file: UploadFile = File(...),
    caption: str = Form(default=""),
    niche: str = Form(default=""),
):
    return await instagram_service.trendy_hashtags(
        video_file=video_file,
        caption=caption,
        niche=niche,
    )


@router.post("/groq_caption")
async def groq_caption(text: str = Form(...)):
    return await instagram_service.groq_caption(text=text)


@router.post("/groq_hashtags")
async def groq_hashtags(text: str = Form(...)):
    return await instagram_service.groq_hashtags(text=text)


@router.post("/transcribe")
async def transcribe_video(
    video_file: UploadFile = File(...),
    target_language: str = Form(default="en"),
):
    return await instagram_service.transcribe_video(
        video_file=video_file,
        target_language=target_language,
    )


@router.post("/extract_hook")
async def extract_hook(video_file: UploadFile = File(...)):
    return await instagram_service.extract_hook(video_file=video_file)
