import random
import asyncio
import instaloader
from stem.control import Controller
from fastapi import Form, HTTPException, Query
from pydantic import constr
from tenacity import retry, stop_after_attempt, wait_exponential
from api import app
from config import TOR_PASSWORD
import requests
from api.db import get_connection
from mysql.connector import Error
from datetime import datetime
import json
import time
import os
import re
import base64
import html
from dotenv import load_dotenv
from typing import Dict, Any, List
from html.parser import HTMLParser
from urllib.parse import quote
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
# import yt_dlp

# ✅ Tor Proxy Configuration
TOR_SOCKS_PROXY = "socks5h://127.0.0.1:9050"
TOR_IP_CHANGE_COOLDOWN = 60  # Prevent changing IP too frequently
last_ip_change_time = 0  # Track last IP change time

# ✅ Global Instaloader Instance (Re-use for efficiency)
loader = instaloader.Instaloader()
load_dotenv()

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
            # raise Exception("⚠️ Video URL not found!")

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
                # raise Exception("⚠️ Still rate-limited after switching Tor IP. Retrying...")

        # raise e  # If other errors, do not retry

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

# ✅ Function to fetch Instagram reels or images snapinsta
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

class _SnapDownloaderParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []
        self.in_row = False
        self.row_div_depth = 0
        self.in_item = False
        self.item_div_depth = 0
        self.in_type_div = False
        self.type_div_depth = 0
        self.in_link = False
        self.link_text_parts = []
        self.link_href = ""
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag != "div" and tag != "a" and tag != "img":
            return

        attrs_dict = dict(attrs)
        cls = attrs_dict.get("class", "") or ""
        classes = set(cls.split())

        if tag == "div":
            if not self.in_row and "row" in classes and "equal" in classes:
                self.in_row = True
                self.row_div_depth = 1
            elif self.in_row:
                self.row_div_depth += 1

            if self.in_row and not self.in_item and "download-item" in classes:
                self.in_item = True
                self.item_div_depth = 1
                self.current = {"type_text": "", "thumbnail": "", "links": []}
            elif self.in_item:
                self.item_div_depth += 1

            if self.in_item and "type" in classes:
                self.in_type_div = True
                self.type_div_depth = 1
            elif self.in_type_div:
                self.type_div_depth += 1

        if self.in_item and tag == "img":
            src = attrs_dict.get("src")
            if src:
                self.current["thumbnail"] = src

        if self.in_item and tag == "a":
            href = attrs_dict.get("href")
            if href and "btn-download" in classes:
                self.in_link = True
                self.link_text_parts = []
                self.link_href = html.unescape(href)

    def handle_endtag(self, tag):
        if tag == "a" and self.in_link:
            link_text = " ".join(part.strip() for part in self.link_text_parts).strip()
            if self.current is not None and self.link_href:
                self.current["links"].append({
                    "href": self.link_href,
                    "text": link_text
                })
            self.in_link = False
            self.link_text_parts = []
            self.link_href = ""
            return

        if tag != "div":
            return

        if self.in_item:
            self.item_div_depth -= 1
            if self.item_div_depth <= 0:
                if self.current:
                    self.items.append(self.current)
                self.current = None
                self.in_item = False
                self.item_div_depth = 0

        if self.in_type_div:
            self.type_div_depth -= 1
            if self.type_div_depth <= 0:
                self.in_type_div = False
                self.type_div_depth = 0

        if self.in_row:
            self.row_div_depth -= 1
            if self.row_div_depth <= 0:
                self.in_row = False
                self.row_div_depth = 0

    def handle_data(self, data):
        if self.in_link:
            self.link_text_parts.append(data)
            return

        if self.in_item and self.in_type_div:
            text = data.strip()
            if text and not self.current.get("type_text"):
                self.current["type_text"] = text


def fetch_instagram_snapdownloader(insta_url: str) -> Dict[str, Any]:
    try:
        encoded_url = quote(insta_url, safe="")
        api_url = (
            "https://snapdownloader.com/tools/instagram-downloader/1/download"
            f"?url={encoded_url}"
        )

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        response = requests.get(api_url, headers=headers, timeout=30)
        if response.status_code != 200 or not response.text:
            raise Exception("⚠️ SnapDownloader HTML not found!")

        parser = _SnapDownloaderParser()
        parser.feed(response.text)

        if not parser.items:
            raise Exception("⚠️ SnapDownloader items not found!")

        post_data = []
        for item in parser.items:
            links = item.get("links", []) or []
            type_text = (item.get("type_text") or "").strip().lower()
            is_video = "video" in type_text
            if not type_text and any(".mp4" in link.get("href", "").lower() for link in links):
                is_video = True

            chosen = ""
            for link in links:
                href = link.get("href", "")
                lower = href.lower()
                if is_video and ".mp4" in lower:
                    chosen = href
                    break
                if not is_video and (".jpg" in lower or ".jpeg" in lower or ".png" in lower):
                    chosen = href
                    break

            if not chosen and links:
                chosen = links[0].get("href", "")

            if not chosen:
                continue

            thumb_url = ""
            for link in links:
                text = (link.get("text") or "").lower()
                href = link.get("href", "")
                if "thumbnail" in text or "cover" in text:
                    thumb_url = href
                    break

            base_thumb = (item.get("thumbnail") or "").strip()
            final_thumb = thumb_url
            if not is_video and chosen:
                if not final_thumb or base_thumb.lower().startswith("data:image"):
                    final_thumb = chosen
            elif not final_thumb and base_thumb.lower().startswith("data:image") and chosen:
                final_thumb = chosen

            post_data.append({
                "type": "GraphVideo" if is_video else "GraphImage",
                "thumbnail": final_thumb or base_thumb,
                "link": chosen
            })

        if not post_data:
            raise Exception("⚠️ SnapDownloader returned no usable links!")

        return {
            "postData": post_data,
            "username": "",
            "profilePic": "",
            "caption": "",
        }
    except Exception as e:
        print(f"⚠️ SnapDownloader error: {e}")
        raise Exception(status_code=400, detail=str(e))

def update_download_history(device_id: str, status: bool):
    """
    status = "success" or "failure"
    """
    conn = get_connection()
    cursor = conn.cursor()

    try:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Check if record exists
        cursor.execute("SELECT id FROM insta_download_history WHERE device_unique_id = %s", (device_id,))
        row = cursor.fetchone()

        if row:
            # Update counts
            if status == True:
                query = """
                    UPDATE insta_download_history
                    SET backend_success_count = backend_success_count + 1,
                        frontend_failure_count = frontend_failure_count + 1,
                        updated_at = %s
                    WHERE device_unique_id = %s
                """
            else:
                query = """
                    UPDATE insta_download_history
                    SET backend_failure_count = backend_failure_count + 1,
                        frontend_failure_count = frontend_failure_count + 1,
                        updated_at = %s
                    WHERE device_unique_id = %s
                """
            cursor.execute(query, (now, device_id))
        else:
            # Insert new
            if status == True:
                query = """
                    INSERT INTO insta_download_history
                    (device_unique_id, backend_success_count, backend_failure_count, frontend_success_count, frontend_failure_count, created_at, updated_at)
                    VALUES (%s, 1, 0, 0, 1, %s, %s)
                """
            else:
                query = """
                    INSERT INTO insta_download_history
                    (device_unique_id, backend_success_count, backend_failure_count, frontend_success_count, frontend_failure_count, created_at, updated_at)
                    VALUES (%s, 0, 1, 0, 1, %s, %s)
                """
            cursor.execute(query, (device_id, now, now))

        conn.commit()

    except Error as e:
        print("DB Error:", e)


    finally:
        cursor.close()
        conn.close()

# ✅ Function to log day-wise analytics in insta_analytics table only
def log_analytics(fallback_method: str, status: str, count_total: bool = True):
    conn = get_connection()
    cursor = conn.cursor()
    today = datetime.now().strftime('%Y-%m-%d')

    try:
        # Check if today's row exists
        cursor.execute("SELECT id FROM insta_analytics WHERE request_date = %s", (today,))
        row = cursor.fetchone()
        if not row:
            cursor.execute("""
                INSERT INTO insta_analytics (
                    request_date,
                    total_requests,
                    total_success,
                    total_failure,
                    sss_success,
                    sss_failure,
                    apify_success,
                    apify_failure,
                    snapdownloader_success,
                    snapdownloader_failure,
                    saveclip_success,
                    saveclip_failure
                )
                VALUES (%s, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
            """, (today,))
            conn.commit()

        if count_total:
            # Always increment total_requests
            cursor.execute("""
                UPDATE insta_analytics SET total_requests = total_requests + 1 WHERE request_date = %s
            """, (today,))

            # Increment success/failure
            if status == "success":
                cursor.execute("""
                    UPDATE insta_analytics SET total_success = total_success + 1 WHERE request_date = %s
                """, (today,))
            else:
                cursor.execute("""
                    UPDATE insta_analytics SET total_failure = total_failure + 1 WHERE request_date = %s
                """, (today,))

        # Increment fallback-specific
        if fallback_method == "sssinstasave":
            if status == "success":
                cursor.execute("""
                    UPDATE insta_analytics SET sss_success = sss_success + 1 WHERE request_date = %s
                """, (today,))
            else:
                cursor.execute("""
                    UPDATE insta_analytics SET sss_failure = sss_failure + 1 WHERE request_date = %s
                """, (today,))
        elif fallback_method == "apify":
            if status == "success":
                cursor.execute("""
                    UPDATE insta_analytics SET apify_success = apify_success + 1 WHERE request_date = %s
                """, (today,))
            else:
                cursor.execute("""
                    UPDATE insta_analytics SET apify_failure = apify_failure + 1 WHERE request_date = %s
                """, (today,))
        elif fallback_method == "snapdownloader":
            if status == "success":
                cursor.execute("""
                    UPDATE insta_analytics SET snapdownloader_success = snapdownloader_success + 1 WHERE request_date = %s
                """, (today,))
            else:
                cursor.execute("""
                    UPDATE insta_analytics SET snapdownloader_failure = snapdownloader_failure + 1 WHERE request_date = %s
                """, (today,))
        elif fallback_method == "saveclip":
            if status == "success":
                cursor.execute("""
                    UPDATE insta_analytics SET saveclip_success = saveclip_success + 1 WHERE request_date = %s
                """, (today,))
            else:
                cursor.execute("""
                    UPDATE insta_analytics SET saveclip_failure = saveclip_failure + 1 WHERE request_date = %s
                """, (today,))

        conn.commit()
    except Error as e:
        print("Analytics DB Error:", e)
    finally:
        cursor.close()
        conn.close()


# ✅ Function to update frontend success count
def update_frontend_success(device_id: str):
    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    try:
        cursor.execute("SELECT id FROM insta_download_history WHERE device_unique_id = %s", (device_id,))
        row = cursor.fetchone()

        if row:
            # Record exists → update
            query = """
                UPDATE insta_download_history
                SET frontend_success_count = frontend_success_count + 1,
                    updated_at = %s
                WHERE device_unique_id = %s
            """
            cursor.execute(query, (now, device_id))
        else:
            # Insert new row
            query = """
                INSERT INTO insta_download_history
                (device_unique_id, backend_success_count, backend_failure_count, frontend_success_count, frontend_failure_count, created_at, updated_at)
                VALUES (%s, 0, 0, 1, 0, %s, %s)
            """
            cursor.execute(query, (device_id, now, now))

        conn.commit()

    except Error as e:
        print("DB Error (frontend_success):", e)

    finally:
        cursor.close()
        conn.close()

# -----------------------
# Common driver setup
# -----------------------
def setup_driver(headless: bool = True) -> webdriver.Chrome:
    options = webdriver.ChromeOptions()

    if headless:
        options.add_argument("--headless=new")

    # Required for Docker / servers
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")

    # Stability
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--start-maximized")

    # Reduce automation detection
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    in_docker = os.path.exists("/.dockerenv")

    if in_docker:
        # Docker: use Chromium
        options.binary_location = "/usr/bin/chromium"

        service = Service("/usr/bin/chromedriver")

    else:
        # Local / VPS: use real Chrome
        options.binary_location = "/usr/bin/google-chrome"

        # Auto-manage driver
        service = Service(ChromeDriverManager().install())

    driver = webdriver.Chrome(service=service, options=options)

    driver.set_page_load_timeout(90)
    return driver


# -----------------------
# Story / Highlight extractor
# -----------------------
def fetch_story_or_highlight(driver: webdriver.Chrome, insta_url: str, headless=True) -> Dict[str, Any]:
    """Fetch Instagram story or highlight via sssinstagram.com"""
    driver.get("https://sssinstagram.com/")

    try:
        WebDriverWait(driver, 3).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button#onetrust-accept-btn-handler, .fc-cta-consent, .ez-accept-all"))
        ).click()
        print("→ Accepted cookie banner (if present).")
    except Exception:
        pass

    # Detect story or highlight
    if "highlights" in insta_url or "highlight" in insta_url or "aGlnaGxpZ2h0" in insta_url:
        endpoint = "/api/v1/instagram/highlightStories"
        print("→ Detected highlight URL, listening for highlightStories API.")
    else:
        endpoint = "/api/v1/instagram/story"
        print("→ Detected story URL, listening for story API.")

    # Inject JS hook for that endpoint
    hook_js = f"""
    (function() {{
        if (window.__story_cap && window.__story_cap.active) return;
        window.__story_cap = {{ active: true, events: [] }};
        function push(evt) {{
        try {{ window.__story_cap.events.push(evt); }} catch (e) {{}}
        }}
        const target = '{endpoint}';

        const origFetch = window.fetch;
        if (origFetch) {{
        window.fetch = async function(...args) {{
            const url = (args && args[0] && args[0].toString()) || '';
            let reqBody = null;
            try {{ if (args[1] && typeof args[1].body !== 'undefined') reqBody = args[1].body; }} catch(e){{}}
            const res = await origFetch.apply(this, args);
            try {{
            if (url.includes(target)) {{
                const txt = await res.clone().text();
                push({{ kind: 'fetch', url, requestBody: reqBody, responseText: txt, status: res.status }});
            }}
            }} catch(e){{}}
            return res;
        }};
        }}

        (function() {{
        const XO = XMLHttpRequest.prototype.open;
        const XS = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function(method, url) {{
            try {{ this.__url = url; this.__method = method; }} catch(e){{}}
            return XO.apply(this, arguments);
        }};
        XMLHttpRequest.prototype.send = function(body) {{
            try {{ this.__body = body; }} catch(e){{}}
            this.addEventListener('load', function() {{
            try {{
                const url = this.__url || '';
                if (url.includes(target)) {{
                let requestBody = this.__body;
                try {{ if (requestBody && typeof requestBody !== 'string') requestBody = JSON.stringify(requestBody); }} catch(e){{}}
                push({{ kind: 'xhr', url, requestBody, responseText: this.responseText, status: this.status }});
                }}
            }} catch(e){{}}
            }});
            return XS.apply(this, arguments);
        }};
        }})();
    }})();
    """
    driver.execute_script(hook_js)

    # Input URL into the site
    box = WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.CSS_SELECTOR, "#input")))
    box.clear()
    box.send_keys(insta_url)
    time.sleep(0.2)

    # Try clicking submit or pressing Enter
    try:
        clicked = False
        for sel in ["button[type='submit']", "button#submit", "button.btn-primary", "button[aria-label='Convert']"]:
            try:
                btn = driver.find_element(By.CSS_SELECTOR, sel)
                btn.click()
                clicked = True
                break
            except Exception:
                pass
        if not clicked:
            box.send_keys(Keys.ENTER)
            print("→ Pressed Enter in input box.")
    except Exception:
        box.send_keys(Keys.ENTER)

    # Wait for captured API call
    def got_event(drv):
        try:
            evts = drv.execute_script("return (window.__story_cap && window.__story_cap.events) || []")
            if not evts:
                return False
            for e in evts:
                if e.get("responseText"):
                    return e
            return False
        except Exception:
            return False

    evt = WebDriverWait(driver, 90).until(got_event)
    raw_response = evt.get("responseText") or ""

    try:
        data = json.loads(raw_response)
    except Exception:
        s = raw_response
        i1, i2 = s.find('{'), s.rfind('}')
        data = json.loads(s[i1:i2+1]) if i1 != -1 and i2 > i1 else {}

    postData = []
    username = ""
    profilePic = ""
    if isinstance(data, dict) and "result" in data:
        for item in data["result"]:
            user = item.get("user", {}) or {}

            if item.get("video_versions"):
                # --- video ---
                for v in item.get("video_versions", []) or []:
                    url = v.get("url_downloadable") or v.get("url_wrapped") or v.get("url")
                    if url:
                        postData.append({
                            "type": "GraphVideo",
                            "link": url,
                            "thumbnail": item.get("image_versions2", {}).get("candidates", [{}])[0].get("url", "")
                        })
            else:
                # --- images (pick highest width only) ---
                candidates = item.get("image_versions2", {}).get("candidates", []) or []
                if candidates:
                    best_img = max(candidates, key=lambda img: img.get("width", 0))
                    url = best_img.get("url_downloadable") or best_img.get("url_wrapped") or best_img.get("url")
                    if url:
                        postData.append({
                            "type": "GraphImage",
                            "link": url,
                            "thumbnail": url,
                            "width": best_img.get("width", 0)
                        })

            # --- user info ---
            if user.get("username"):
                username = user.get("username")

            if user.get("profile_pic_url") or user.get("profile_pic_url_wrapped") or user.get("profile_pic_url_downloadable"):
                profilePic = (
                    user.get("profile_pic_url_downloadable")
                    or user.get("profile_pic_url_wrapped")
                    or user.get("profile_pic_url")
                )                   

    return {
        "postData": postData,
        "username": username,
        "profilePic": profilePic,
        "caption": "",
    }


# -----------------------
# Main unified fetcher
# -----------------------
def fetch_instagram_sss(insta_url: str, headless: bool = True) -> Dict[str, Any]:

    driver = setup_driver(headless=headless)

    # ---- STEALTH PATCH (MUST BE FIRST) ----
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
            """
        }
    )

    try:
        # Open site
        driver.get("https://sssinstagram.com/")

        # Wait for full load
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

        # Debug screenshot (remove later)
        driver.save_screenshot("/app/debug_sss.png")

        # Accept cookies if shown
        try:
            WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((
                    By.CSS_SELECTOR,
                    "button#onetrust-accept-btn-handler, .fc-cta-consent, .ez-accept-all"
                ))
            ).click()
        except Exception:
            pass

        # ---- Hook API ----
        hook_js = r"""
        (function() {
          if (window.__cap && window.__cap.active) return;

          window.__cap = { active: true, events: [] };

          function push(evt){
            try { window.__cap.events.push(evt); } catch(e){}
          }

          const of = window.fetch;
          if (of){
            window.fetch = async function(...args){
              const res = await of.apply(this,args);
              try{
                const url = (args && args[0] && args[0].toString()) || '';
                if(url.includes('/api/convert')){
                  const txt = await res.clone().text();
                  push({url:url,data:txt,status:res.status});
                }
              }catch(e){}
              return res;
            }
          }

          const XO = XMLHttpRequest.prototype.open;
          const XS = XMLHttpRequest.prototype.send;

          XMLHttpRequest.prototype.open = function(m,u){
            this.__u = u;
            return XO.apply(this,arguments);
          }

          XMLHttpRequest.prototype.send = function(b){
            this.addEventListener('load',function(){
              try{
                if((this.__u||'').includes('/api/convert')){
                  push({url:this.__u,data:this.responseText,status:this.status});
                }
              }catch(e){}
            });
            return XS.apply(this,arguments);
          }
        })();
        """

        driver.execute_script(hook_js)

        # Input box
        box = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#input"))
        )

        box.clear()
        time.sleep(0.5)

        box.send_keys(insta_url)
        time.sleep(0.5)
        box.send_keys(Keys.ENTER)

        # Wait for API response
        def got_data(drv):
            try:
                evts = drv.execute_script(
                    "return (window.__cap && window.__cap.events)||[]"
                )
                for e in evts:
                    if e.get("data"):
                        return e
                return False
            except Exception:
                return False

        evt = WebDriverWait(driver, 90).until(got_data)

        raw = evt.get("data")

        if not raw:
            raise Exception("No API data received")

        data = json.loads(raw)

        if isinstance(data, dict):
            data = [data]

        postData = []
        username = ""
        caption = ""

        for item in data:

            urls = item.get("url") or []
            thumb = item.get("thumb", "")
            meta = item.get("meta") or {}

            for u in urls:
                ext = (u.get("ext") or "").lower()

                media_type = "GraphVideo" if ext == "mp4" else "GraphImage"

                postData.append({
                    "type": media_type,
                    "thumbnail": thumb,
                    "link": u.get("url")
                })

            username = meta.get("username", username)
            caption = meta.get("title", caption)

        if not postData:
            raise Exception("Empty media list (likely blocked)")

        return {
            "postData": postData,
            "username": username,
            "profilePic": "",
            "caption": caption
        }

    finally:
        try:
            driver.quit()
        except Exception:
            pass

# ---------- Helper ----------
def get_active_apify_key():
    conn = get_connection()
    if not conn:
        return None
    try:
        with conn.cursor(dictionary=True, buffered=True) as cur:
            cur.execute("SELECT token FROM apify_keys WHERE is_active=1 AND is_disabled=0 LIMIT 1")
            r = cur.fetchone()
            if r:
                return r["token"]
            cur.execute("SELECT token FROM apify_keys WHERE is_disabled=0 ORDER BY (max_amount_limit - current_balance) DESC LIMIT 1")
            r = cur.fetchone()
            return r["token"] if r else None
    finally:
        conn.close()

# ✅ Function to fetch Instagram media via Apify Instagram Post Scraper
def fetch_apify_instagram_post(url: str) -> dict:
    # Read Apify token from DB first, fallback to environment variable
    token = get_active_apify_key() or os.getenv("APIFY_TOKEN")
    if not token:
        print("⚠️ Apify token not configured in DB or env; skipping Apify fallback")
        return None
    api_url = "https://api.apify.com/v2/acts/apify~instagram-post-scraper/run-sync-get-dataset-items?token=" + token
    payload = {
        "username": [url],
        "resultsLimit": 1
    }
    headers = {"Content-Type": "application/json"}
    try:
        resp = requests.post(api_url, json=payload, headers=headers, timeout=60)
    except requests.exceptions.Timeout:
        print("⚠️ Apify request timed out; retrying once...")
        resp = requests.post(api_url, json=payload, headers=headers, timeout=60)
    data = resp.json()
    if not data or not isinstance(data, list):
        return None

    post = data[0]
    # Sidecar handling
    sidecar = []
    if post.get("type", "").lower() == "sidecar" and "childPosts" in post:
        for child in post["childPosts"]:
            media_type = "GraphVideo" if child.get("type", "").lower() == "video" else "GraphImage"
            sidecar.append({
                "type": media_type,
                "thumbnail": child.get("displayUrl"),
                "link": child.get("videoUrl") if media_type == "GraphVideo" else child.get("displayUrl")
            })
    elif post.get("type", "").lower() == "video":
        sidecar.append({
            "type": "GraphVideo",
            "thumbnail": post.get("displayUrl"),
            "link": post.get("videoUrl")
        })
    elif post.get("type", "").lower() == "image":
        sidecar.append({
            "type": "GraphImage",
            "thumbnail": post.get("displayUrl"),
            "link": post.get("displayUrl")
        })

    return {
        "postData": sidecar,
        "username": post.get("ownerUsername", ""),
        "profilePic": "",
        "caption": post.get("caption", "")
    }


def fetch_sss_profile_posts(insta_url: str, headless: bool = True) -> dict:
    """
    Fetch the latest profile post via sssinstagram UI (captures /api/v1/instagram/postsV2 network call).
    Only the first post is returned to match existing response structure.
    """
    def safe_json_load(text: str):
        try:
            return json.loads(text)
        except Exception:
            s = text or ""
            i1, i2 = s.find("{"), s.rfind("}")
            if i1 != -1 and i2 > i1:
                return json.loads(s[i1 : i2 + 1])
            raise

    driver = setup_driver(headless=headless)
    try:
        driver.get("https://sssinstagram.com/")

        try:
            WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((
                    By.CSS_SELECTOR,
                    "button#onetrust-accept-btn-handler, .fc-cta-consent, .ez-accept-all"
                ))
            ).click()
        except Exception:
            pass

        # Hook into fetch/xhr for posts endpoints (prefer postsV2, fallback to posts)
        hook_js = r"""
        (function() {
          if (window.__prof_cap && window.__prof_cap.active) return;
          window.__prof_cap = { active: true, events: [] };
          const targets = ['/api/v1/instagram/postsV2', '/api/v1/instagram/posts'];

          function push(evt) { try { window.__prof_cap.events.push(evt); } catch(e) {} }
          function matchTarget(url) {
            try {
              for (const t of targets) { if (url.includes(t)) return t; }
            } catch(e) {}
            return null;
          }

          const of = window.fetch;
          if (of) {
            window.fetch = async function(...args) {
              const res = await of.apply(this, args);
              try {
                const url = (args && args[0] && args[0].toString()) || '';
                const m = matchTarget(url);
                if (m) {
                  const txt = await res.clone().text();
                  push({kind: 'fetch', url, matched: m, dataText: txt, ok: res.ok, status: res.status});
                }
              } catch(e) {}
              return res;
            };
          }

          const XO = XMLHttpRequest.prototype.open;
          const XS = XMLHttpRequest.prototype.send;
          XMLHttpRequest.prototype.open = function(method, url) {
            this.__prof_url = url;
            return XO.apply(this, arguments);
          };
          XMLHttpRequest.prototype.send = function(body) {
            this.addEventListener('load', function() {
              try {
                const url = this.__prof_url || '';
                const m = matchTarget(url);
                if (m) {
                  push({kind: 'xhr', url: url, matched: m, dataText: this.responseText, ok: (this.status>=200 && this.status<300), status: this.status});
                }
              } catch(e) {}
            });
            return XS.apply(this, arguments);
          };
        })();
        """
        driver.execute_script(hook_js)

        box = WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.CSS_SELECTOR, "#input")))
        box.clear()
        box.send_keys(insta_url)
        box.send_keys(Keys.ENTER)

        def get_events(drv):
            try:
                return drv.execute_script("return (window.__prof_cap && window.__prof_cap.events)||[]")
            except Exception:
                return []

        def find_event(evts, needle: str):
            for e in evts:
                matched = e.get("matched")
                if matched:
                    if matched != needle:
                        continue
                else:
                    if needle not in (e.get("url") or ""):
                        continue
                if e.get("dataText"):
                    return e
            return None

        def got_any_profile_evt(drv):
            evts = get_events(drv)
            v2 = find_event(evts, "/api/v1/instagram/postsV2")
            if v2:
                return v2
            p = find_event(evts, "/api/v1/instagram/posts")
            if p:
                return p
            return False

        def parse_posts_v2_payload(raw_text: str) -> dict:
            data = safe_json_load(raw_text)
            result = None
            if isinstance(data, dict):
                result = data.get("result") if isinstance(data.get("result"), dict) else data
            elif isinstance(data, list) and data:
                first = data[0]
                result = first.get("result") if isinstance(first, dict) else first

            edges = (result or {}).get("edges") or []
            if not edges:
                raise ValueError("No posts returned from postsV2")

            first_node = edges[0].get("node") if isinstance(edges[0], dict) else None
            if not first_node:
                raise ValueError("Invalid postsV2 response shape: missing node")

            postData: List[Dict[str, Any]] = []

            def add_media(node: Dict[str, Any]):
                typename = node.get("__typename", "")
                is_video = node.get("is_video", False)

                if typename == "GraphSidecar" and node.get("edge_sidecar_to_children"):
                    for child in node["edge_sidecar_to_children"].get("edges", []):
                        add_media((child or {}).get("node", {}))
                    return

                if typename == "GraphVideo" or is_video:
                    link = node.get("video_url_downloadable") or node.get("video_url") or node.get("display_url")
                    thumb = node.get("thumbnail_src") or node.get("display_url")
                    if link:
                        postData.append({"type": "GraphVideo", "thumbnail": thumb or link, "link": link})
                else:
                    link = node.get("display_url") or node.get("thumbnail_src")
                    thumb = node.get("thumbnail_src") or link
                    if link:
                        postData.append({"type": "GraphImage", "thumbnail": thumb or link, "link": link})

            add_media(first_node)

            owner = first_node.get("owner") or {}
            username = owner.get("username") or (result or {}).get("username", "")
            profile_pic = (
                owner.get("profile_pic_url")
                or (result or {}).get("profile_pic_url")
                or (result or {}).get("profilePic")
            )

            caption = ""
            caption_edges = first_node.get("edge_media_to_caption", {}).get("edges", [])
            if caption_edges:
                caption = (caption_edges[0].get("node") or {}).get("text", "")

            return {
                "postData": postData,
                "username": username,
                "profilePic": profile_pic or "",
                "caption": caption,
            }

        def parse_posts_payload(raw_text: str) -> dict:
            data = safe_json_load(raw_text)
            result = data.get("result") if isinstance(data, dict) else None
            if not isinstance(result, dict):
                raise ValueError("Invalid posts response shape: missing result")

            edges = result.get("edges") or []
            if not edges:
                raise ValueError("No posts returned from posts")

            first_node = edges[0].get("node") if isinstance(edges[0], dict) else None
            if not isinstance(first_node, dict):
                raise ValueError("Invalid posts response shape: missing node")

            def best_by_width(items):
                if not items:
                    return None
                return max(items, key=lambda x: (x.get("width") or x.get("config_width") or 0))

            def pick_url(obj: Dict[str, Any]):
                return obj.get("url_downloadable") or obj.get("url_wrapped") or obj.get("url")

            def image_url_from(node: Dict[str, Any]):
                cands = ((node.get("image_versions2") or {}).get("candidates") or [])
                best = best_by_width(cands)
                if best:
                    return pick_url(best) or best.get("url")
                return node.get("display_url")

            def video_url_from(node: Dict[str, Any]):
                versions = node.get("video_versions") or []
                best = best_by_width(versions)
                if best:
                    return pick_url(best) or best.get("url")
                return None

            postData: List[Dict[str, Any]] = []

            def add_media(node: Dict[str, Any]):
                carousel = node.get("carousel_media") or []
                if carousel:
                    for item in carousel:
                        if isinstance(item, dict):
                            add_media(item)
                    return

                video_url = video_url_from(node)
                if video_url:
                    thumb = image_url_from(node) or video_url
                    postData.append({"type": "GraphVideo", "thumbnail": thumb, "link": video_url})
                    return

                img_url = image_url_from(node)
                if img_url:
                    postData.append({"type": "GraphImage", "thumbnail": img_url, "link": img_url})

            add_media(first_node)

            user = first_node.get("user") or {}
            username = user.get("username") or ""
            profile_pic = user.get("profile_pic_url") or ""
            caption = ""
            cap = first_node.get("caption")
            if isinstance(cap, dict):
                caption = cap.get("text") or ""

            return {
                "postData": postData,
                "username": username,
                "profilePic": profile_pic,
                "caption": caption,
            }

        evt = WebDriverWait(driver, 60).until(got_any_profile_evt)
        raw = evt.get("dataText") or ""

        if "/api/v1/instagram/postsV2" in (evt.get("matched") or evt.get("url") or ""):
            try:
                return parse_posts_v2_payload(raw)
            except Exception:
                evts = get_events(driver)
                p_evt = find_event(evts, "/api/v1/instagram/posts")
                if not p_evt:
                    p_evt = WebDriverWait(driver, 30).until(lambda d: find_event(get_events(d), "/api/v1/instagram/posts") or False)
                return parse_posts_payload(p_evt.get("dataText") or "")

        return parse_posts_payload(raw)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

def fetch_instagram_saveclip(insta_url: str, headless: bool = True) -> Dict[str, Any]:
    """Fetch Instagram media via saveclip.app using Selenium"""
    driver = setup_driver(headless=headless)

    # Apply stealth patch
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
            """
        }
    )

    try:
        # Navigate to saveclip.app
        driver.get("https://saveclip.app/en")
        print("→ Opened saveclip.app")

        # Wait for page to load
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

        # Accept cookies if present
        try:
            WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((
                    By.CSS_SELECTOR,
                    "button#onetrust-accept-btn-handler, .fc-cta-consent, .ez-accept-all, button[class*='accept']"
                ))
            ).click()
            print("→ Accepted cookies")
        except Exception:
            print("→ No cookie banner or already accepted")
            pass

        # Find input field and enter URL
        input_field = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input#s_input, input[name='q']"))
        )
        input_field.clear()
        input_field.send_keys(insta_url)
        time.sleep(0.5)
        print(f"→ Entered URL: {insta_url}")

        # Click download button
        download_btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button.btn-default, button[onclick*='ksearchvideo']"))
        )
        download_btn.click()
        print("→ Clicked download button")

        # Wait for download items to appear
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".download-items"))
        )
        print("→ Download items loaded")

        time.sleep(2)  # Give extra time for all items to render

        # Get all download items
        download_items = driver.find_elements(By.CSS_SELECTOR, ".download-items")
        print(f"→ Found {len(download_items)} download items")

        postData = []

        for idx, item in enumerate(download_items):
            try:
                # Get thumbnail
                thumbnail = ""
                try:
                    thumb_img = item.find_element(By.CSS_SELECTOR, ".download-items__thumb img")
                    thumbnail = thumb_img.get_attribute("src") or ""
                except Exception:
                    pass

                # Check if it's a video or image based on format icon
                is_video = False
                try:
                    format_icon = item.find_element(By.CSS_SELECTOR, ".format-icon i")
                    icon_class = format_icon.get_attribute("class") or ""
                    is_video = "video" in icon_class.lower()
                except Exception:
                    pass

                # Try to get download link - Priority order:
                # 1. Direct link from <a> tag with id pattern photo_dl_* or video_dl_*
                # 2. Select dropdown first option value
                # 3. Any link from download button
                download_link = ""

                # Try Method 1: Find link using photo_id pattern from select onchange
                # (Only if select.minimal dropdown exists - some posts don't have quality options)
                try:
                    select_element = item.find_element(By.CSS_SELECTOR, "select.minimal")
                    link_id = select_element.get_attribute("onchange") or ""
                    # Extract ID from onchange like "getPhotoLink('3564263038514907871', this);"
                    id_match = re.search(r"get(?:Photo|Video)Link\('([^']+)'", link_id)
                    if id_match:
                        photo_id = id_match.group(1)
                        # Try to find the corresponding download link
                        for id_prefix in ["photo_dl_", "video_dl_", "dl_"]:
                            try:
                                link_element = item.find_element(By.CSS_SELECTOR, f"a#{id_prefix}{photo_id}")
                                dl = link_element.get_attribute("href") or ""
                                if dl and ("dl.snapcdn.app" in dl or ".mp4" in dl or ".jpg" in dl or ".jpeg" in dl or ".png" in dl):
                                    download_link = dl
                                    break
                            except Exception:
                                continue
                except Exception:
                    # select.minimal not found - this is expected for posts without quality options
                    pass

                # Try Method 2: Get from select dropdown first option
                # (Only if select.minimal dropdown exists - some posts don't have quality options)
                if not download_link:
                    try:
                        select_element = item.find_element(By.CSS_SELECTOR, "select.minimal")
                        options = select_element.find_elements(By.TAG_NAME, "option")
                        if options:
                            opt_value = options[0].get_attribute("value") or ""
                            # Make sure it's a download link, not a thumbnail
                            if opt_value and ("dl.snapcdn.app" in opt_value or ".mp4" in opt_value or ".jpg" in opt_value):
                                download_link = opt_value
                    except Exception:
                        # select.minimal not found - this is expected for posts without quality options
                        pass

                # Try Method 3: Direct link from download button (skip thumbnail, get video)
                if not download_link:
                    try:
                        # Find ALL download buttons within this item
                        link_elements = item.find_elements(By.CSS_SELECTOR, ".download-items__btn a")
                        for link_element in link_elements:
                            title = (link_element.get_attribute("title") or "").lower()
                            dl = link_element.get_attribute("href") or ""

                            # Skip thumbnail buttons
                            if "thumbnail" in title:
                                continue

                            # Prioritize video links
                            if "video" in title and dl:
                                download_link = dl
                                print(f"→ Found video link from download button (title: {title})")
                                break

                            # Fallback to any valid download link that's not a thumbnail
                            if dl and ("dl.snapcdn.app" in dl or ".mp4" in dl or ".jpg" in dl or ".jpeg" in dl or ".png" in dl):
                                download_link = dl
                                print(f"→ Found link from download button (title: {title})")
                    except Exception as e:
                        print(f"→ Method 3 failed: {e}")
                        pass

                # Final validation: Make sure download link is not the same as thumbnail
                if download_link and thumbnail and download_link == thumbnail:
                    print(f"⚠️ Warning: Download link same as thumbnail, skipping")
                    download_link = ""

                if download_link:
                    # Determine media type
                    media_type = "GraphVideo" if is_video or ".mp4" in download_link.lower() else "GraphImage"

                    postData.append({
                        "type": media_type,
                        "thumbnail": thumbnail,
                        "link": download_link
                    })
                    # print(f"→ Item {idx + 1}: {media_type}")
                    # print(f"   Thumbnail: {thumbnail[:80]}...")
                    # print(f"   Link: {download_link[:80]}...")
                else:
                    print(f"⚠️ No valid download link found for item {idx + 1}")

            except Exception as e:
                print(f"⚠️ Error processing item {idx + 1}: {e}")
                continue

        if not postData:
            raise Exception("⚠️ No download links found on saveclip.app")

        return {
            "postData": postData,
            "username": "",
            "profilePic": "",
            "caption": "",
        }

    except Exception as e:
        print(f"⚠️ SaveClip error: {e}")
        raise Exception(f"SaveClip error: {str(e)}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def normalize_instagram_url(insta_url: str) -> str:
    """Normalize and validate an Instagram URL (reel, post, story, highlight, or profile)."""

    # 1️⃣ Resolve /share/ redirect
    if "/share/" in insta_url:
        try:
            response = requests.head(insta_url, allow_redirects=True, timeout=10)
            insta_url = response.url
        except Exception as e:
            raise ValueError(f"❌ Failed to resolve share link: {e}")

    # 2️⃣ Decode /s/ base64 Instagram app links
    match = re.search(r"/s/([^/?#]+)", insta_url)
    if match:
        encoded_part = match.group(1)
        try:
            padding = "=" * (-len(encoded_part) % 4)
            decoded_bytes = base64.b64decode(encoded_part + padding)
            decoded_text = decoded_bytes.decode("utf-8", errors="ignore")

            # Extract highlight ID if present
            highlight_match = re.search(r"highlight:(\d+)", decoded_text)
            if highlight_match:
                highlight_id = highlight_match.group(1)
                insta_url = f"https://www.instagram.com/stories/highlights/{highlight_id}/"
        except Exception as e:
            print(f"⚠️ Base64 decode failed: {e}")

    # 3️⃣ Remove tracking/query parameters
    clean_url = insta_url.split("?")[0].split("#")[0].rstrip("/")

    # 4️⃣ Valid URL patterns
    valid_patterns = [
        r"^https?://(www\.)?instagram\.com/reel/[A-Za-z0-9_-]+$",
        r"^https?://(www\.)?instagram\.com/[A-Za-z0-9_.]+/reel/[A-Za-z0-9_-]+/?$",
        r"^https?://(www\.)?instagram\.com/p/[A-Za-z0-9_-]+$",
        r"^https?://(www\.)?instagram\.com/[A-Za-z0-9_.]+/p/[A-Za-z0-9_-]+/?$",
        r"^https?://(www\.)?instagram\.com/stories/[^/]+/\d+$",
        r"^https?://(www\.)?instagram\.com/[A-Za-z0-9_.]+/stories/[A-Za-z0-9_-]+/?$",
        r"^https?://(www\.)?instagram\.com/stories/highlights/\d+$",
        r"^https?://(www\.)?instagram\.com/[A-Za-z0-9_.]+/stories/highlights/[A-Za-z0-9_-]+/?$",
        r"^https?://(www\.)?instagram\.com/tv/[A-Za-z0-9_-]+$",
        r"^https?://(www\.)?instagram\.com/[A-Za-z0-9_.]+/tv/[A-Za-z0-9_-]+/?$",
    ]

    channel_valid_patterns = [r"^https?://(www\.)?instagram\.com/[A-Za-z0-9_.]+$"]
    if any(re.match(p, clean_url) for p in channel_valid_patterns):
        return {"code": 200, "data": clean_url}

    if not any(re.match(p, clean_url) for p in valid_patterns):
        print(f"❌ Invalid Instagram URL: {clean_url}")
        return {"code": 400, "message": "The link you entered isn’t valid. Please verify it and try again."}

    return clean_url

# ✅ FastAPI Endpoint to Download Instagram Media
@app.post("/download_media")
async def download_media(instagramURL: str = Form(...), deviceId: str = Form(min_length=1)):

    print(f"🔍 Fetching actual media for URL: {instagramURL} | Device ID: {deviceId}")
    clean_url = normalize_instagram_url(instagramURL)
    if isinstance(clean_url, dict):  # Error case
        if clean_url.get("code") == 200:
            print(f"🔍 media URL is profile URL: {clean_url}")
            try:
                media_details = fetch_instagram_saveclip(clean_url.get("data"))
                update_download_history(deviceId, True)
                log_analytics("saveclip", "success")
                print(f"saveclip profile success")
                return {"code": 200, "data": media_details}
            except Exception as e:
                print(f"⚠️ Error in saveclip profile fetch: {e}")
                log_analytics("saveclip", "failure", count_total=False)
                pass

            try:
                media_details = fetch_instagram_snapdownloader(clean_url.get("data"))
                update_download_history(deviceId, True)
                log_analytics("snapdownloader", "success")
                print(f"snapdownloader profile success")
                return {"code": 200, "data": media_details}
            except HTTPException:
                log_analytics("snapdownloader", "failure", count_total=False)
                pass
            except Exception as e:
                print(f"⚠️ Error in snapdownloader profile fetch: {e}")
                log_analytics("snapdownloader", "failure", count_total=False)
                pass

            # try:
            #     media_details = fetch_sss_profile_posts(clean_url.get("data"))
            #     update_download_history(deviceId, True)
            #     log_analytics("sssinstasave", "success")
            #     print(f"sssinstasave profile success")
            #     return {"code": 200, "data": media_details}
            # except Exception as e:
            #     print(f"⚠️ Error in SSS profile fetch: {e}")
            #     log_analytics("sssinstasave", "failure", count_total=False)

            try:
                media_details = fetch_apify_instagram_post(clean_url.get("data"))
                if not media_details or not media_details.get("postData"):
                    raise ValueError("Apify returned empty data")
                update_download_history(deviceId, True)
                log_analytics("apify", "success")
                print(f"apify profile success")
                return {"code": 200, "data": media_details}
            except Exception as e:
                print(f"⚠️ Error in Apify fallback: {e}")
                update_download_history(deviceId, False)
                log_analytics("apify", "failure")
                return {"code": 200, "data": None, "message": "Media cannot be fetched. Please try again later."}
        else:    
            return clean_url
    print(f"🔍 Fetching clean media for URL: {clean_url} | Device ID: {deviceId}")
    # exit()

    # try:
    #     change_tor_ip()
    #     media_details = fetch_instagram_media(clean_url, use_tor=True)
    #     await asyncio.sleep(random.uniform(2, 5))
    #     update_download_history(deviceId, True)
    #     if isinstance(media_details, dict):  # ✅ only if it's a dict
    #         return {"code": 200, "data": media_details}
    #     else:
    #         pass 
    # except Exception:
    #     pass

    # Fallback 0: saveclip
    try:
        media_details = fetch_instagram_saveclip(clean_url)
        update_download_history(deviceId, True)
        log_analytics("saveclip", "success")
        print(f"saveclip post success")
        return {"code": 200, "data": media_details}
    except HTTPException:
        log_analytics("saveclip", "failure", count_total=False)
        pass
    except Exception as e:
        print(f"⚠️ Error in saveclip: {e}")
        log_analytics("saveclip", "failure", count_total=False)
        pass

    # Fallback 1: snapdownloader
    try:
        media_details = fetch_instagram_snapdownloader(clean_url)
        update_download_history(deviceId, True)
        log_analytics("snapdownloader", "success")
        print(f"snapdownloader post success")
        return {"code": 200, "data": media_details}
    except HTTPException:
        log_analytics("snapdownloader", "failure", count_total=False)
        pass
    except Exception as e:
        print(f"⚠️ Error in snapdownloader: {e}")
        log_analytics("snapdownloader", "failure", count_total=False)
        pass

    # Fallback 2: sssinstasave
    # try:
    #     media_details = fetch_instagram_sss(clean_url)
    #     update_download_history(deviceId, True)
    #     log_analytics("sssinstasave", "success")
    #     print(f"sssinstasave post success")
    #     return {"code": 200, "data": media_details}
    # except HTTPException:
    #     log_analytics("sssinstasave", "failure", count_total=False)
    #     pass
    # except Exception as e:
    #     print(f"⚠️ Error in sssinstasave: {e}")
    #     log_analytics("sssinstasave", "failure", count_total=False)
    #     pass

    # Fallback 3: Apify
    try:
        media_details = fetch_apify_instagram_post(instagramURL)
        if not media_details or not media_details.get("postData"):
            raise ValueError("Apify returned empty data")
        update_download_history(deviceId, True)
        log_analytics("apify", "success")
        print(f"apify post success")
        return {"code": 200, "data": media_details}
    except Exception as e:
        print(f"⚠️ Error in Apify fallback: {e}")
        update_download_history(deviceId, False)
        log_analytics("apify", "failure")
        return {"code": 200, "data": None, "message": "Media cannot be fetched. Please try again later."}

    
@app.post("/frontend_success")
async def frontend_success(deviceId: str = Form(...)):
    try:
        deviceId = deviceId.replace(" ", "")
        update_frontend_success(deviceId)
        return {"code": 200, "message": "Frontend success count updated"}

    except Exception as e:
        return {"code": 500, "data": None, "message": str(e)}

# ✅ Run FastAPI with Uvicorn (Development Mode)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000)
