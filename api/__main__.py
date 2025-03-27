import random
import asyncio
import instaloader
from stem.control import Controller
from fastapi import Form, HTTPException
from tenacity import retry, stop_after_attempt, wait_exponential
from api import app
from config import TOR_PASSWORD
import requests
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
            # Wait for a few seconds for the new IP to be assigned
            time.sleep(2)

            # Fetch the new IP address
            new_ip = get_tor_ip()
            print(f"🌍 New Tor IP Address: {new_ip}")
            last_ip_change_time = current_time  # Update last change time
    except Exception as e:
        print(f"⚠️ Error changing Tor IP: {e}")

def get_tor_ip():
    """Fetch the current public IP address through Tor."""
    try:
        # Send a request through Tor to get the current IP
        session = requests.Session()
        session.proxies = {"http": "socks5h://127.0.0.1:9050", "https": "socks5h://127.0.0.1:9050"}
        response = session.get("https://check.torproject.org/api/ip")
        return response.json().get("IP")  # Get the 'ip' field from the response
    except Exception as e:
        print(f"⚠️ Error fetching Tor IP: {e}")
        return None        

# ✅ Function to fetch Instagram reels, images, or carousel posts
@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=3, max=30))
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
        print(f"✅ Found post type: {post.typename}")
        print(f"✅ Found post thumbnail: {post._full_metadata_dict['thumbnail_src']}")
        print(f"✅ Found post username: {post.owner_username}")
        print(f"✅ Found post profilePic: {post._full_metadata_dict['owner']['profile_pic_url']}")
        print(f"✅ Found post caption: {post.caption}")

        if not post:
            raise Exception("⚠️ Post not found!")

        # Determine media type and return URLs
        if post.is_video:
            post_data = {
                "postData": [
                    {
                        "type": post.typename,
                        "thumbnail": post._full_metadata_dict['thumbnail_src'],
                        "link": post.video_url
                    }
                ],
                "username": post.owner_username,  # Username of the post owner
                "profilePic": post._full_metadata_dict['owner']['profile_pic_url'],  # Profile picture URL of the user
                "caption": post.caption,  # Caption of the post
            }
            return post_data
        elif post.typename == "GraphImage":
            post_data = {
                "postData": [
                    {
                        "type": post.typename,
                        "thumbnail": post._full_metadata_dict['thumbnail_src'],
                        "link": post.url
                    }
                ],
                "username": post.owner_username,  # Username of the post owner
                "profilePic": post._full_metadata_dict['owner']['profile_pic_url'],  # Profile picture URL of the user
                "caption": post.caption,  # Caption of the post
            }
            return post_data
        elif post.typename == "GraphSidecar":
            postData = [
                {
                    "type": "GraphVideo" if node.is_video else "GraphImage",
                    "thumbnail": node.display_url,
                    "link": node.video_url if node.is_video else node.display_url
                }
                for node in post.get_sidecar_nodes()
            ]
            post_data = {
                "postData": postData,
                "username": post.owner_username,  # Username of the post owner
                "profilePic": post._full_metadata_dict['owner']['profile_pic_url'],  # Profile picture URL of the user
                "caption": post.caption,  # Caption of the post
            }
            return post_data
        else:
            post_data = {
                "postData": [],
                "username": '',
                "profilePic": '',
                "caption": '',
            }
            return post_data   
        
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

# ✅ Function to fetch Instagram reels or images
@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=30))
def fetch_instagram_data(url):
    try:
        data = {"url": url}
        response = requests.post("https://snapinsta.app/get-data.php", data=data)
        resData = response.json()
        print(resData)

        if response.status_code != 200 or not resData:
            raise Exception("⚠️ Post not found!")

        # Convert SnapInsta JSON response to our format

        postData = [
            {
                "type": post["__type"],
                "thumbnail": post["preview_url"] if post["__type"] == "GraphImage" else post["thumbnail_url"],
                "link": post["download_url"] if post["__type"] == "GraphImage" else post["video_url"]
            }
            for post in resData["files"]
        ]

        return {
            "postData": postData,
            "username": resData["user_info"]["username"],
            "profilePic": resData["user_info"]["avatar_url"],
            "caption": '',
        }
    except Exception as e:
        print(f"⚠️ SnapInsta error: {e}")
        raise Exception(status_code=400, detail=str(e))

# ✅ FastAPI Endpoint to Download Instagram Media
@app.post("/download_media")
async def download_media(instagramURL: str = Form(...)):
    try:
        clean_url = instagramURL.split("/?")[0]  # Clean up URL

        # ✅ Fetch media WITHOUT Tor first, retry with Tor if needed
        change_tor_ip()
        media_details = fetch_instagram_media(clean_url, use_tor=True)

        # ✅ Introduce a small random delay (2-5 seconds)
        await asyncio.sleep(random.uniform(2, 5))

        return {"code": 200, "data": media_details}

    except HTTPException as e:
        #raise e  # Return FastAPI error with status code
        media_details = fetch_instagram_data(clean_url)
        return {"code": 200, "data": media_details}

    except Exception as e:
        #raise HTTPException(status_code=400, detail=str(e))
        media_details = fetch_instagram_data(clean_url)
        return {"code": 200, "data": media_details}

# ✅ Run FastAPI with Uvicorn (Development Mode)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000)
