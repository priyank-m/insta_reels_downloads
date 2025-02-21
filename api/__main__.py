import random
import asyncio
import yt_dlp
from stem.control import Controller
from fastapi import FastAPI, Form, HTTPException
from tenacity import retry, stop_after_attempt, wait_exponential
from api import app
from config import TOR_PASSWORD

TOR_SOCKS_PROXY = "socks5h://127.0.0.1:9050"

# ✅ Function to change Tor IP safely (Only change when rate-limited)
def change_tor_ip():
    try:
        with Controller.from_port(port=9051) as controller:
            controller.authenticate(password=TOR_PASSWORD)
            controller.signal("NEWNYM")  # Request new IP
            print("✅ Tor IP changed successfully!")
    except Exception as e:
        print(f"⚠️ Error changing Tor IP: {e}")

# ✅ Function to fetch Instagram reels (Retry if rate-limited)
@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=3, max=30))
def fetch_reel_url(clean_url, use_tor=False):
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Mobile Safari/537.36"
    }

    ydl_opts = {
        "quiet": True,
        "skip_download": True,  # Only extract URL, don't download
        "format": "best",
        "nocheckcertificate": True,
        "http_headers": headers,
    }

    if use_tor:
        ydl_opts["proxy"] = TOR_SOCKS_PROXY  # Use Tor if enabled

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(clean_url, download=False)
            video_url = info_dict.get("url")

        if not video_url:
            raise Exception("⚠️ Video URL not found!")

        return video_url

    except Exception as e:
        error_message = str(e).lower()

        # ✅ If not using Tor and rate-limit error is detected, retry with Tor
        if "too many requests" in error_message or "rate limit" in error_message or "429" in error_message:
            if not use_tor:
                print("⚠️ Rate limit detected! Switching to Tor...")
                change_tor_ip()  # Rotate IP before switching to Tor
                return fetch_reel_url(clean_url, use_tor=True)
            else:
                print("⚠️ Tor is also rate-limited! Retrying with a new IP...")
                change_tor_ip()  # Rotate IP again
                raise Exception("⚠️ Still rate-limited after switching Tor IP. Retrying...")

        raise e  # If other errors, do not retry

@app.post("/download_reel")
async def download_reel(reelURL: str = Form(...)):
    try:
        clean_url = reelURL.split("/?")[0]

        # ✅ Fetch reel first WITHOUT Tor, retry with Tor if needed
        video_url = fetch_reel_url(clean_url)

        # ✅ Introduce a small random delay (prevents Instagram rate-limiting)
        await asyncio.sleep(random.uniform(1, 2))

        return {"code": 200, "video_url": video_url}

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# ✅ Run FastAPI with Uvicorn
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="127.0.0.1", port=8000)
