import os
import time
import random
import yt_dlp
import requests
from stem.control import Controller
from fastapi import FastAPI, Form, HTTPException
from tenacity import retry, stop_after_attempt, wait_exponential
from api import app

TOR_SOCKS_PROXY = "socks5h://127.0.0.1:9050"

# ✅ Function to change Tor IP safely
def change_tor_ip():
    try:
        with Controller.from_port(port=9051) as controller:
            controller.authenticate(password="my_secure_password")  # Use your real password
            controller.signal("NEWNYM")  # Correct signal to change Tor IP
            print("✅ Tor IP changed successfully!")
    except Exception as e:
        print(f"⚠️ Error changing Tor IP: {e}")

# ✅ Function to fetch Instagram reels with retries
@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=5, max=60))
def fetch_reel_url(clean_url):
    change_tor_ip()  # Rotate IP before request

    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Mobile Safari/537.36"
    }

    ydl_opts = {
        "quiet": True,
        "skip_download": True,  # Only extract URL, don't download
        "format": "best",
        "nocheckcertificate": True,
        "proxy": TOR_SOCKS_PROXY,
        "http_headers": headers,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(clean_url, download=False)
        video_url = info_dict.get("url")

    if not video_url:
        raise Exception("⚠️ Video URL not found!")

    return video_url

@app.post("/download_reel")
async def download_reel(reelURL: str = Form(...)):
    try:
        clean_url = reelURL.split("/?")[0]

        # ✅ Fetch reel with automatic retries and IP rotation
        video_url = fetch_reel_url(clean_url)

        # ✅ Introduce a random delay to prevent rate-limiting
        time.sleep(random.uniform(2, 3))

        return {"code": 200, "video_url": video_url}

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="127.0.0.1", port=8000)
