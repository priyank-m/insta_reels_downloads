import random
import asyncio
import instaloader
from stem.control import Controller
from fastapi import Form, HTTPException
from tenacity import retry, stop_after_attempt, wait_exponential
from api import app
from config import TOR_PASSWORD
# import yt_dlp

# ✅ Tor Proxy Configuration
TOR_SOCKS_PROXY = "socks5h://127.0.0.1:9050"
TOR_IP_CHANGE_COOLDOWN = 60  # Prevent changing IP too frequently
last_ip_change_time = 0  # Track last IP change time

# ✅ Global Instaloader Instance (Re-use for efficiency)
loader = instaloader.Instaloader()

# ✅ Function to change Tor IP (With Cooldown)
def change_tor_ip():
    global last_ip_change_time

    import time
    current_time = time.time()

    # Check if cooldown has passed before changing IP
    if current_time - last_ip_change_time < TOR_IP_CHANGE_COOLDOWN:
        print("⏳ Waiting before changing Tor IP to avoid rate-limit triggers...")
        return

    try:
        with Controller.from_port(port=9051) as controller:
            controller.authenticate(password=TOR_PASSWORD)
            controller.signal("NEWNYM")  # Request new IP
            print("✅ Tor IP changed successfully!")
            last_ip_change_time = current_time  # Update last change time
    except Exception as e:
        print(f"⚠️ Error changing Tor IP: {e}")

# ✅ Function to fetch Instagram reels, images, or carousel posts
@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=3, max=30))
def fetch_instagram_media(clean_url, use_tor=False):
    # using ydl package to fetch the video url
    # headers = {
    #     "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Mobile Safari/537.36"
    # }

    # ydl_opts = {
    #     "quiet": True,
    #     "skip_download": True,  # Only extract URL, don't download
    #     "format": "best",
    #     "nocheckcertificate": True,
    #     "http_headers": headers,
    # }

    # if use_tor:
    #     ydl_opts["proxy"] = TOR_SOCKS_PROXY  # Use Tor if enabled

    # try:
    #     with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    #         info_dict = ydl.extract_info(clean_url, download=False)
    #         video_url = info_dict.get("url")

    #     if not video_url:
    #         raise Exception("⚠️ Video URL not found!")

    #     return video_url

    # except Exception as e:
    #     error_message = str(e).lower()

    #     # ✅ If not using Tor and rate-limit error is detected, retry with Tor
    #     if "too many requests" in error_message or "rate limit" in error_message or "429" in error_message:
    #         if not use_tor:
    #             print("⚠️ Rate limit detected! Switching to Tor...")
    #             change_tor_ip()  # Rotate IP before switching to Tor
    #             return fetch_reel_url(clean_url, use_tor=True)
    #         else:
    #             print("⚠️ Tor is also rate-limited! Retrying with a new IP...")
    #             change_tor_ip()  # Rotate IP again
    #             raise Exception("⚠️ Still rate-limited after switching Tor IP. Retrying...")

    #     raise e  # If other errors, do not retry

    # using instaloader package to fetch the Reel, Image, Video, or Carousel url
    try:
        # Extract shortcode from URL
        shortcode = clean_url.strip("/").split("/")[-1]

        # Use global Instaloader instance
        global loader

        # Set proxy if using Tor
        if use_tor:
            loader.context.proxy = TOR_SOCKS_PROXY

        # Fetch post details
        post = instaloader.Post.from_shortcode(loader.context, shortcode)
        print(f"✅ Found post: {post}")

        if not post:
            raise Exception("⚠️ Post not found!")

        # Determine media type and return URLs
        if post.is_video:
            return post.video_url
        elif post.typename == "GraphImage":
            return post.url
        elif post.typename == "GraphSidecar":
            media_urls = [node.display_url if not node.is_video else node.video_url for node in post.get_sidecar_nodes()]
            return media_urls
        else:
            return ''
        
    except instaloader.exceptions.TwoFactorAuthRequiredException:
        raise HTTPException(status_code=401, detail="⚠️ Two-factor authentication is required.")
    except instaloader.exceptions.InstaloaderException as e:
        print(f"⚠️ Instaloader specific error: {e}")

        error_message = str(e).lower()

        # Handle 401 Unauthorized or rate-limited errors here
        if "too many queries" in error_message or "rate limit" in error_message or "429" in error_message or "401" in error_message:
            if not use_tor:
                print("⚠️ Rate limit detected! Switching to Tor...")
                change_tor_ip()
                return fetch_instagram_media(clean_url, use_tor=True)
            else:
                print("⚠️ Tor is also rate-limited! Retrying with a new IP...")
                change_tor_ip()
                raise HTTPException(status_code=429, detail="⚠️ Still rate-limited after switching Tor IP. Retrying...")

        # Handle other exceptions
        raise HTTPException(status_code=500, detail=f"⚠️ An error occurred: {str(e)}")    

# ✅ FastAPI Endpoint to Download Instagram Media
@app.post("/download_media")
async def download_media(instagramURL: str = Form(...)):
    try:
        clean_url = instagramURL.split("/?")[0]  # Clean up URL

        # ✅ Fetch media WITHOUT Tor first, retry with Tor if needed
        change_tor_ip()
        media_details = fetch_instagram_media(clean_url, use_tor=True)

        # ✅ Introduce a small random delay (2-5 seconds)
        await asyncio.sleep(random.uniform(1, 2))

        return {"code": 200, "data": media_details}

    except HTTPException as e:
        raise e  # Return FastAPI error with status code

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# ✅ Run FastAPI with Uvicorn (Development Mode)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000)
