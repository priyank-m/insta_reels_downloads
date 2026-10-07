import random
import asyncio
import subprocess
import instaloader
from stem.control import Controller
from fastapi import File, Form, HTTPException, Query, UploadFile
from pydantic import constr
from tenacity import retry, stop_after_attempt, wait_exponential
from app.core.config import settings
TOR_PASSWORD = settings.tor_password
import requests
from app.db.session import get_connection
from app.repositories.apify_key_repository import decrypt_apify_key_row, ensure_apify_token_encryption
from app.repositories.instagram_service_repository import get_download_service_settings
from app.repositories.settings_repository import get_setting
from mysql.connector import Error
from datetime import datetime
import json
import time
import os
import re
import base64
import html
import string
from dotenv import load_dotenv
from typing import Callable, Dict, Any, List
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
import urllib.request
import http.cookiejar
from typing import Optional
import yt_dlp
import tempfile

try:
    from google import genai
    from google.genai import types
    _gemini_client = None

    def _get_gemini():
        global _gemini_client
        if _gemini_client is None:
            key = get_setting("GEMINI_API_KEY", os.getenv("GEMINI_API_KEY", ""))
            if not key:
                raise ValueError("GEMINI_API_KEY not configured")
            _gemini_client = genai.Client(api_key=key)
        return _gemini_client
except ImportError:
    _get_gemini = None

# ✅ Tor Proxy Configuration
TOR_SOCKS_PROXY = "socks5h://127.0.0.1:9050"
TOR_IP_CHANGE_COOLDOWN = 60  # Prevent changing IP too frequently
last_ip_change_time = 0  # Track last IP change time
TOR_CONTROL_PORT = 9051

# ✅ Global Instaloader Instance (Re-use for efficiency)
# loader = instaloader.Instaloader()
# load_dotenv()

def get_tor_session():
    session = requests.Session()

    session.proxies = {
        "http": TOR_SOCKS_PROXY,
        "https": TOR_SOCKS_PROXY
    }

    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Connection": "close"   # VERY IMPORTANT (no socket reuse)
    })

    return session

# ✅ Function to change Tor IP (With Cooldown)
def change_tor_ip():
    global last_ip_change_time

    now = time.time()
    if now - last_ip_change_time < 8:
        return

    try:
        with Controller.from_port(port=TOR_CONTROL_PORT) as controller:
            controller.authenticate()
            controller.signal("NEWNYM")

        print("🔄 Tor new circuit requested")
        time.sleep(18)   # IMPORTANT

        last_ip_change_time = time.time()

    except Exception as e:
        print("Tor change failed:", e)         

def reset_instagram_identity():
    """Clear cookies + force urllib to use Tor"""
    cj = http.cookiejar.CookieJar()

    proxy_handler = urllib.request.ProxyHandler({
        "http": TOR_SOCKS_PROXY,
        "https": TOR_SOCKS_PROXY
    })

    opener = urllib.request.build_opener(
        proxy_handler,
        urllib.request.HTTPCookieProcessor(cj)
    )

    urllib.request.install_opener(opener)

def create_loader(use_tor: bool):
    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        save_metadata=False,
        compress_json=False,
        max_connection_attempts=1
    )

    if use_tor:
        L.context.proxy = TOR_SOCKS_PROXY

    return L

def check_instagram_privacy(url: str, use_tor: Optional[bool] = False) -> str:
    """
    Rule:
        'No Media Match' in response -> private
        anything else -> public
    """

    session = requests.Session()
    OEMBED_URL = "https://www.instagram.com/api/v1/oembed/"

    if use_tor:
        session.proxies = {"http": TOR_SOCKS_PROXY, "https": TOR_SOCKS_PROXY}

    try:
        # small human delay (keeps IG happy)
        time.sleep(random.uniform(0.4, 1.0))

        r = session.get(
            OEMBED_URL,
            params={"url": url},
            timeout=20,
        )

        text = (r.text or "").lower()

        if "no media match" in text:
            return "private"

        return "public"

    except Exception:
        # fail-open
        return "public"

    finally:
        session.close()

class _InstagramMetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.description = ""
        self.twitter_title = ""
        self.og_url = ""
        self.canonical_url = ""

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "meta":
            meta_key = attrs_dict.get("property") or attrs_dict.get("name")
            content = html.unescape(attrs_dict.get("content", "") or "").strip()
            if meta_key == "og:description" and not self.description:
                self.description = content
            elif meta_key == "twitter:title" and not self.twitter_title:
                self.twitter_title = content
            elif meta_key == "og:url" and not self.og_url:
                self.og_url = content
        elif tag == "link" and attrs_dict.get("rel") == "canonical" and not self.canonical_url:
            self.canonical_url = html.unescape(attrs_dict.get("href", "") or "").strip()

def _clean_instagram_url(raw_url: str) -> str:
    parts = urlsplit(raw_url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

def _extract_hashtags(text: str) -> List[str]:
    return re.findall(r"#\w+", text or "")

def _clean_caption_text(text: str) -> str:
    caption = re.sub(r"#\w+", "", text or "").strip()
    caption = re.sub(r"\n{3,}", "\n\n", caption)
    return re.sub(r"[ \t]+", " ", caption).strip()

def _parse_instagram_og_description(raw_caption: str) -> Dict[str, Any]:
    raw_caption = html.unescape(raw_caption or "").strip()
    if not raw_caption:
        return {"username": "", "caption": "", "hashtags": []}

    username = ""
    prefix = re.match(
        r"^[\d,.KMkm]+\s+likes?,\s*[\d,.KMkm]+\s+comments?\s*-\s*"
        r"([A-Za-z0-9._]+)\s+on\s+.*?:\s*[\"“]?",
        raw_caption,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if prefix:
        username = prefix.group(1)
        text = raw_caption[prefix.end():].strip()
    else:
        user_match = re.search(
            r"likes?,\s*[\d,.KMkm]+\s+comments?\s*-\s*([A-Za-z0-9._]+)\s+on\s+",
            raw_caption,
            flags=re.IGNORECASE,
        )
        username = user_match.group(1) if user_match else ""
        text = raw_caption

    text = text.rstrip("\"”").strip()

    return {
        "username": username,
        "caption": _clean_caption_text(text),
        "hashtags": _extract_hashtags(text),
    }

def fetch_instagram_og_metadata(instagram_url: str) -> Dict[str, Any]:
    parser = fetch_instagram_page_metadata(instagram_url)
    metadata = _parse_instagram_og_description(parser.description)
    if metadata.get("username") or metadata.get("caption") or metadata.get("hashtags"):
        return metadata

    return {"username": "", "caption": "", "hashtags": []}

_instagram_page_metadata_cache: Dict[str, _InstagramMetaParser] = {}

def fetch_instagram_page_metadata(instagram_url: str) -> _InstagramMetaParser:
    clean_url = _clean_instagram_url(instagram_url)
    if clean_url in _instagram_page_metadata_cache:
        return _instagram_page_metadata_cache[clean_url]

    user_agents = [
        "Mozilla/5.0",
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 "
            "Mobile/15E148 Safari/604.1"
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    ]

    last_error = None
    for user_agent in user_agents:
        try:
            response = requests.get(
                clean_url,
                headers={
                    "User-Agent": user_agent,
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=20,
            )
            response.raise_for_status()

            parser = _InstagramMetaParser()
            parser.feed(response.text or "")
            if parser.description or parser.twitter_title or parser.og_url or parser.canonical_url:
                _instagram_page_metadata_cache[clean_url] = parser
                return parser
        except Exception as e:
            last_error = e

    if last_error:
        raise last_error

    parser = _InstagramMetaParser()
    _instagram_page_metadata_cache[clean_url] = parser
    return parser

def enrich_instagram_metadata(media_details: Dict[str, Any], instagram_url: str) -> Dict[str, Any]:
    if not isinstance(media_details, dict) or not media_details.get("postData"):
        return media_details

    enriched = dict(media_details)
    if "hashtags" not in enriched:
        enriched["hashtags"] = _extract_hashtags(enriched.get("caption", ""))

    needs_scrape = not enriched.get("username") or not enriched.get("caption")
    if not needs_scrape:
        return enriched

    try:
        fallback = fetch_instagram_og_metadata(instagram_url)
    except Exception as e:
        print(f"⚠️ Instagram metadata fallback error: {e}")
        return enriched

    if not enriched.get("username") and fallback.get("username"):
        enriched["username"] = fallback["username"]
    if not enriched.get("caption") and fallback.get("caption"):
        enriched["caption"] = fallback["caption"]
    if not enriched.get("hashtags") and fallback.get("hashtags"):
        enriched["hashtags"] = fallback["hashtags"]

    return enriched

def _instagram_path(insta_url: str) -> str:
    return urlsplit(insta_url or "").path

def _is_instagram_image_post_url(insta_url: str) -> bool:
    return "/p/" in _instagram_path(insta_url)

def _is_instagram_photo_post_url(insta_url: str) -> bool:
    if not _is_instagram_image_post_url(insta_url):
        return False

    try:
        metadata = fetch_instagram_page_metadata(insta_url)
    except Exception as e:
        print(f"⚠️ Instagram photo metadata check failed: {e}")
        return False

    canonical_path = _instagram_path(metadata.canonical_url)
    og_path = _instagram_path(metadata.og_url)
    title = (metadata.twitter_title or "").lower()

    if "/reel/" in canonical_path or "/reel/" in og_path or "instagram reel" in title:
        return False
    if "/tv/" in canonical_path or "/tv/" in og_path or "instagram video" in title:
        return False

    return "/p/" in canonical_path and "instagram photo" in title and "instagram photos and videos" not in title

def _is_instagram_video_url(insta_url: str) -> bool:
    path = _instagram_path(insta_url)
    return "/reel/" in path or "/tv/" in path

def _is_threads_url(media_url: str) -> bool:
    return "threads." in urlsplit(media_url or "").netloc.lower()

def fetch_instagram_oembed_post(insta_url: str) -> Dict[str, Any]:
    response = requests.get(
        "https://www.instagram.com/api/v1/oembed/",
        params={"url": insta_url},
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()

    caption = html.unescape(data.get("title", "") or "").strip()
    thumbnail = data.get("thumbnail_url", "") or ""
    if not thumbnail:
        raise ValueError("Instagram oEmbed returned no thumbnail_url")

    return {
        "postData": [{
            "type": "GraphImage",
            "thumbnail": thumbnail,
            "link": thumbnail,
        }],
        "username": data.get("author_name", "") or "",
        "profilePic": "",
        "caption": _clean_caption_text(caption),
        "hashtags": _extract_hashtags(caption),
    }

def fetch_instagram_rapidapi_provider(media_url: str) -> Dict[str, Any]:
    rapidapi_key = get_setting("RAPIDAPI_KEY", settings.rapidapi_key)
    if not rapidapi_key:
        raise ValueError("RAPIDAPI_KEY not configured")

    endpoint = _rapidapi_endpoint_for_url(media_url)
    api_url = f"https://{settings.rapidapi_instagram_host}/{endpoint}"
    response = requests.get(
        api_url,
        params={"url": media_url},
        headers={
            "Content-Type": "application/json",
            "x-rapidapi-host": settings.rapidapi_instagram_host,
            "x-rapidapi-key": rapidapi_key,
        },
        timeout=45,
    )
    response.raise_for_status()
    data = response.json()

    post_data = _rapidapi_extract_post_data(data)
    if not post_data:
        raise ValueError("RapidAPI returned no media items")

    caption = _first_nested_value(data, ("caption", "title", "description", "text")) or ""
    username = _first_nested_value(data, ("username", "user_name", "short_name")) or ""
    profile_pic = _first_nested_value(
        data,
        ("profile_pic_url", "profile_pic_url_hd", "profilePic", "avatar", "avatar_url"),
    ) or ""

    return {
        "postData": post_data,
        "username": username,
        "profilePic": profile_pic,
        "caption": caption,
        "hashtags": _extract_hashtags(caption),
    }


DOWNLOADGRAM_API_URL = "https://api.downloadgram.org/media"
DOWNLOADGRAM_ORIGIN = "https://downloadgram.org"
DOWNLOADGRAM_CDN_HOST = "cdn.downloadgram.org"
DOWNLOADGRAM_MEDIA_HOST_SUFFIXES = (".cdninstagram.com", ".fbcdn.net")


class _DownloadGramParser(HTMLParser):
    """Collect DownloadGram preview/download pairs from its returned markup."""

    def __init__(self):
        super().__init__()
        self.items: List[Dict[str, str]] = []
        self.current_thumbnail = ""

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "img":
            source = attrs_dict.get("src", "").strip()
            if source:
                self.current_thumbnail = source
            return

        if tag != "a":
            return

        link = attrs_dict.get("href", "").strip()
        if link:
            self.items.append({
                "thumbnail": self.current_thumbnail,
                "link": link,
            })


def _decode_downloadgram_markup(response_text: str) -> str:
    """Decode the limited JavaScript escaping used around DownloadGram HTML."""
    decoded = re.sub(
        r"\\x([0-9a-fA-F]{2})",
        lambda match: chr(int(match.group(1), 16)),
        response_text or "",
    )
    return decoded.replace(r"\/", "/").replace(r'\"', '"').replace(r"\'", "'")


def _downloadgram_url(raw_url: str) -> str:
    normalized = urljoin(DOWNLOADGRAM_ORIGIN, html.unescape(raw_url or "").strip())
    parsed = urlsplit(normalized)
    if parsed.scheme != "https" or parsed.hostname != DOWNLOADGRAM_CDN_HOST:
        return ""
    return normalized


def _downloadgram_original_url(download_url: str) -> str:
    token = parse_qs(urlsplit(download_url).query).get("token", [""])[0]
    parts = token.split(".")
    if len(parts) < 2:
        return ""

    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return ""

    return data.get("url", "") if isinstance(data, dict) else ""


def _downloadgram_stream_url(download_url: str) -> str:
    """Return the token's direct media URL only when it is an expected CDN host."""
    original_url = _downloadgram_original_url(download_url)
    parsed = urlsplit(original_url)
    hostname = (parsed.hostname or "").lower()
    is_media_host = hostname.endswith(DOWNLOADGRAM_MEDIA_HOST_SUFFIXES)
    if parsed.scheme != "https" or not is_media_host:
        return ""
    return original_url


def _downloadgram_media_type(download_url: str) -> str:
    original_url = _downloadgram_original_url(download_url)
    extension = _rapidapi_url_extension(original_url or download_url)
    return "GraphVideo" if extension in {"mp4", "mov", "m4v", "webm", "m3u8"} else "GraphImage"


def _parse_downloadgram_response(response_text: str) -> List[Dict[str, str]]:
    parser = _DownloadGramParser()
    parser.feed(_decode_downloadgram_markup(response_text))

    post_data: List[Dict[str, str]] = []
    seen = set()
    for item in parser.items:
        wrapper_url = _downloadgram_url(item["link"])
        stream_url = _downloadgram_stream_url(wrapper_url)
        if not stream_url or stream_url in seen:
            continue
        seen.add(stream_url)

        thumbnail_wrapper = _downloadgram_url(item["thumbnail"])
        thumbnail = _downloadgram_stream_url(thumbnail_wrapper) or stream_url
        post_data.append({
            "type": _downloadgram_media_type(wrapper_url),
            "thumbnail": thumbnail,
            "link": stream_url,
        })

    return post_data


def fetch_instagram_downloadgram(insta_url: str) -> Dict[str, Any]:
    """Fetch direct media links from DownloadGram's public form endpoint."""
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:155.0) Gecko/20100101 Firefox/155.0",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": DOWNLOADGRAM_ORIGIN,
        "Referer": f"{DOWNLOADGRAM_ORIGIN}/",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    response = requests.post(
        DOWNLOADGRAM_API_URL,
        data={"url": insta_url, "v": "3", "lang": "en"},
        headers=headers,
        timeout=45,
    )
    response.raise_for_status()

    post_data = _parse_downloadgram_response(response.text)
    if not post_data:
        raise ValueError("DownloadGram returned no usable media links")

    return {
        "postData": post_data,
        "username": "",
        "profilePic": "",
        "caption": "",
    }

def _rapidapi_endpoint_for_url(media_url: str) -> str:
    parsed = urlsplit(media_url or "")
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    if "threads." in host:
        return "getThreads"
    if "/stories/highlights/" in path:
        return "getHighlight"
    if "/stories/" in path:
        return "getStory"
    return "getPost"

def _rapidapi_extract_post_data(payload: Any) -> List[Dict[str, str]]:
    structured_items = _rapidapi_extract_structured_media(payload)
    if structured_items:
        return structured_items

    items: List[Dict[str, str]] = []
    seen = set()

    def add_item(link: str, thumbnail: str = "", media_type: str = "") -> None:
        if not link or link in seen or not _rapidapi_is_media_url(link):
            return

        seen.add(link)
        media_type = media_type or ("GraphVideo" if _rapidapi_is_video_url(link) else "GraphImage")
        items.append({
            "type": media_type,
            "thumbnail": thumbnail or link,
            "link": link,
        })

    def walk(node: Any, inherited_thumb: str = "") -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, inherited_thumb)
            return

        if not isinstance(node, dict):
            return

        thumb = (
            _first_direct_value(node, ("thumbnail", "thumbnail_url", "thumb", "preview", "cover", "display_url"))
            or _rapidapi_image_candidate(node)
            or inherited_thumb
        )

        video_versions = node.get("video_versions")
        if isinstance(video_versions, list):
            for version in video_versions:
                if isinstance(version, dict):
                    add_item(version.get("url", ""), thumb, "GraphVideo")

        image_url = _rapidapi_image_candidate(node)
        if image_url and not video_versions:
            add_item(image_url, thumb or image_url, "GraphImage")

        downloads = node.get("downloads")
        if isinstance(downloads, list):
            for download in downloads:
                if isinstance(download, dict):
                    link = _first_direct_value(download, ("url", "download_url", "link"))
                    kind = str(download.get("kind") or download.get("type") or "").lower()
                    media_type = "GraphVideo" if kind == "video" else ""
                    add_item(link, thumb, media_type)

        direct_link = _first_direct_value(
            node,
            (
                "video_url",
                "videoUrl",
                "download_url",
                "downloadUrl",
                "media_url",
                "mediaUrl",
                "url",
                "display_url",
                "image_url",
                "imageUrl",
            ),
        )
        typename = str(node.get("type") or node.get("kind") or node.get("__typename") or "").lower()
        media_type = "GraphVideo" if "video" in typename or _rapidapi_is_video_url(direct_link) else "GraphImage"
        add_item(direct_link, thumb, media_type)

        for value in node.values():
            if isinstance(value, (dict, list)):
                walk(value, thumb)

    walk(payload)
    return items

def _rapidapi_extract_structured_media(payload: Any) -> List[Dict[str, str]]:
    if not isinstance(payload, dict):
        return []

    media_nodes = payload.get("media")
    if isinstance(media_nodes, list) and media_nodes:
        nodes = [node for node in media_nodes if isinstance(node, dict)]
    else:
        nodes = [payload]

    items: List[Dict[str, str]] = []
    seen = set()
    for node in nodes:
        item = _rapidapi_media_node_to_post_data(node)
        if not item:
            continue
        link = item["link"]
        if link in seen:
            continue
        seen.add(link)
        items.append(item)

    return items

def _rapidapi_media_node_to_post_data(node: Dict[str, Any]) -> Optional[Dict[str, str]]:
    media_type_text = str(node.get("type") or node.get("__typename") or "").lower()
    is_video = bool(node.get("is_video")) or "video" in media_type_text

    thumbnail = (
        _first_direct_value(node, ("thumbnail_src", "thumbnail", "thumbnail_url", "src", "display_url"))
        or _rapidapi_display_resource(node)
        or _rapidapi_image_candidate(node)
    )

    if is_video:
        link = _first_direct_value(node, ("video_url", "videoUrl", "download_url", "media_url"))
        if link:
            return {
                "type": "GraphVideo",
                "thumbnail": thumbnail or link,
                "link": link,
            }

    link = (
        _rapidapi_display_resource(node)
        or _first_direct_value(node, ("display_url", "src", "image_url", "media_url", "url"))
        or _rapidapi_image_candidate(node)
    )
    if link:
        return {
            "type": "GraphVideo" if _rapidapi_is_video_url(link) else "GraphImage",
            "thumbnail": thumbnail or link,
            "link": link,
        }

    return None

def _rapidapi_display_resource(node: Dict[str, Any]) -> str:
    resources = node.get("display_resources")
    if not isinstance(resources, list) or not resources:
        return ""

    best = None
    best_width = -1
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        src = resource.get("src")
        width = int(resource.get("config_width") or 0)
        if src and width >= best_width:
            best = src
            best_width = width

    return best or ""

def _rapidapi_image_candidate(node: Dict[str, Any]) -> str:
    versions = node.get("image_versions2")
    if isinstance(versions, dict):
        candidates = versions.get("candidates")
        if isinstance(candidates, list) and candidates:
            first = candidates[0]
            if isinstance(first, dict):
                return first.get("url", "") or ""
    return ""

def _first_direct_value(node: Dict[str, Any], keys: tuple) -> str:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value:
            return value
    return ""

def _first_nested_value(payload: Any, keys: tuple) -> str:
    if isinstance(payload, list):
        for item in payload:
            value = _first_nested_value(item, keys)
            if value:
                return value
        return ""

    if not isinstance(payload, dict):
        return ""

    direct = _first_direct_value(payload, keys)
    if direct:
        return direct

    for value in payload.values():
        nested = _first_nested_value(value, keys)
        if nested:
            return nested
    return ""

def _rapidapi_is_video_url(url: str) -> bool:
    return _rapidapi_url_extension(url) in {"mp4", "mov", "m3u8"}

def _rapidapi_is_media_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    extension = _rapidapi_url_extension(url)
    if "instagram.com/" in url and extension not in {"mp4", "mov", "m3u8", "jpg", "jpeg", "png", "webp"}:
        return False
    return url.startswith("http") and bool(
        extension in {"mp4", "mov", "m3u8", "jpg", "jpeg", "png", "webp"}
        or "cdninstagram.com" in url
        or "fbcdn.net" in url
    )

def _rapidapi_url_extension(url: str) -> str:
    path = urlsplit(url or "").path.lower()
    match = re.search(r"\.([a-z0-9]+)$", path)
    return match.group(1) if match else ""

def fetch_instagram_ytdlp_video(insta_url: str) -> Dict[str, Any]:
    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "noplaylist": False,
        "format": "best",
        "nocheckcertificate": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(insta_url, download=False)

    entries = info.get("entries") if isinstance(info, dict) else None
    items = list(entries) if entries else [info]
    post_data = []

    for item in items:
        if not isinstance(item, dict):
            continue

        media_url = item.get("url") or item.get("webpage_url")
        thumbnail = item.get("thumbnail") or media_url
        if not media_url:
            continue

        ext = (item.get("ext") or "").lower()
        vcodec = item.get("vcodec")
        is_video = ext == "mp4" or (vcodec and vcodec != "none")

        post_data.append({
            "type": "GraphVideo" if is_video else "GraphImage",
            "thumbnail": thumbnail,
            "link": media_url,
        })

    if not post_data:
        raise ValueError("yt-dlp returned no media URL")

    caption = info.get("description") or info.get("title") or ""
    username = info.get("uploader_id") or info.get("uploader") or info.get("channel") or ""

    return {
        "postData": post_data,
        "username": username,
        "profilePic": "",
        "caption": _clean_caption_text(caption),
        "hashtags": _extract_hashtags(caption),
    }

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
        shortcode = clean_url.strip("/").split("/")[-1]

        loader = create_loader(use_tor)

        post = instaloader.Post.from_shortcode(loader.context, shortcode)

        print("✅ Post:", post.typename, post.owner_username)

        base = {
            "username": post.owner_username,
            "profilePic": post._full_metadata_dict['owner']['profile_pic_url'],
            "caption": post.caption,
        }

        # VIDEO
        if post.is_video:
            return {
                **base,
                "postData": [{
                    "type": post.typename,
                    "thumbnail": post._full_metadata_dict['thumbnail_src'],
                    "link": post.video_url
                }]
            }

        # IMAGE
        elif post.typename == "GraphImage":
            return {
                **base,
                "postData": [{
                    "type": post.typename,
                    "thumbnail": post._full_metadata_dict['thumbnail_src'],
                    "link": post.url
                }]
            }

        # CAROUSEL
        elif post.typename == "GraphSidecar":
            items = []
            for node in post.get_sidecar_nodes():
                items.append({
                    "type": "GraphVideo" if node.is_video else "GraphImage",
                    "thumbnail": node.display_url,
                    "link": node.video_url if node.is_video else node.display_url
                })

            return {**base, "postData": items}

        return {"postData": [], "username": "", "profilePic": "", "caption": ""}

    # ---------------- ERROR HANDLING ----------------

    except instaloader.exceptions.InstaloaderException as e:

        error = str(e).lower()
        print("⚠️ Instagram error:", error)

        if any(x in error for x in ["rate limit", "too many queries", "429", "401"]):

            if not use_tor:
                print("Switching to Tor identity...")
                change_tor_ip()
                reset_instagram_identity()
                time.sleep(10)
                return fetch_instagram_media(clean_url, True)

            else:
                print("Tor identity burned. Cooling down...")
                change_tor_ip()
                reset_instagram_identity()
                time.sleep(25)
                raise HTTPException(status_code=429, detail="Instagram rate limit reached")

        raise HTTPException(status_code=500, detail=str(e))    

SNAPINSTA_BASE_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ+/"

def _snapinsta_base_convert(value: str, source_base: int, target_base: int) -> str:
    source_digits = SNAPINSTA_BASE_ALPHABET[:source_base]
    target_digits = SNAPINSTA_BASE_ALPHABET[:target_base]
    number = 0

    for power, char in enumerate(reversed(value)):
        if char in source_digits:
            number += source_digits.index(char) * (source_base ** power)

    if number == 0:
        return "0"

    result = ""
    while number > 0:
        result = target_digits[number % target_base] + result
        number //= target_base
    return result

def _snapinsta_extract_data_field(text: str) -> str:
    text = (text or "").strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "data" in obj:
            return obj["data"]
    except Exception:
        pass

    match = re.search(r'"data"\s*:\s*"(.+)"\s*}', text, re.DOTALL)
    if match:
        return match.group(1).encode("utf-8").decode("unicode_escape")
    return text

def _snapinsta_find_eval_arguments(js_code: str):
    pattern = re.compile(
        r'eval\s*\(\s*function\s*\([^)]*\)\s*\{.*?\}'
        r'\s*\(\s*"([^"]+)"\s*,\s*(\d+)\s*,\s*"([^"]+)"\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)\s*\)',
        re.DOTALL,
    )
    match = pattern.search(js_code or "")
    if not match:
        raise ValueError("Could not find SnapInsta encoded eval payload")

    return (
        match.group(1),
        int(match.group(2)),
        match.group(3),
        int(match.group(4)),
        int(match.group(5)),
    )

def _snapinsta_decode_obfuscated_js(raw_response: str) -> str:
    js_code = _snapinsta_extract_data_field(raw_response)
    encoded_text, _unused, marker_alphabet, offset, source_base = _snapinsta_find_eval_arguments(js_code)
    delimiter = marker_alphabet[source_base]
    decoded = ""
    i = 0

    while i < len(encoded_text):
        chunk = ""
        while i < len(encoded_text) and encoded_text[i] != delimiter:
            chunk += encoded_text[i]
            i += 1

        for index, marker in enumerate(marker_alphabet):
            chunk = chunk.replace(marker, str(index))

        if chunk:
            char_code = int(_snapinsta_base_convert(chunk, source_base, 10)) - offset
            decoded += chr(char_code)

        i += 1

    return unquote(decoded)

def _snapinsta_decode_token_url(raw_url: str) -> str:
    token_match = re.search(r"[?&]token=([^\"'&<>\s]+)", html.unescape(raw_url or ""))
    if not token_match:
        return html.unescape(raw_url or "")

    token = token_match.group(1)
    parts = token.split(".")
    if len(parts) < 2:
        return html.unescape(raw_url or "")

    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode("utf-8"))
        return html.unescape(data.get("url") or raw_url or "")
    except Exception:
        return html.unescape(raw_url or "")

def _snapinsta_parse_decoded_html(decoded_html: str) -> List[Dict[str, str]]:
    items = []
    blocks = re.findall(r'<div class=\\"download-items\\">(.*?)</div></li>', decoded_html or "", flags=re.DOTALL)
    if not blocks:
        blocks = re.findall(r'<div class="download-items">(.*?)</div></li>', decoded_html or "", flags=re.DOTALL)

    for block in blocks:
        normalized = block.replace('\\"', '"')
        preview_match = re.search(r'<img[^>]+src="([^"]+)"', normalized, flags=re.DOTALL)
        preview = _snapinsta_decode_token_url(preview_match.group(1)) if preview_match else ""

        is_video = "icon-dlvideo" in normalized or "Download Video" in normalized
        candidate_urls = re.findall(r'(?:href|value)="([^"]+)"', normalized)
        media_url = ""

        for candidate in candidate_urls:
            decoded_url = _snapinsta_decode_token_url(candidate)
            lower = decoded_url.lower()
            if is_video and ".mp4" in lower:
                media_url = decoded_url
                break
            if not is_video and any(ext in lower for ext in (".jpg", ".jpeg", ".png", ".webp")):
                media_url = decoded_url
                break

        if not media_url and candidate_urls:
            media_url = _snapinsta_decode_token_url(candidate_urls[0])

        if not media_url:
            continue

        items.append({
            "type": "GraphVideo" if is_video or ".mp4" in media_url.lower() else "GraphImage",
            "thumbnail": preview or media_url,
            "link": media_url,
        })

    return items

# ✅ Function to fetch Instagram reels or images snapinsta
@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=30))
def fetch_instagram_data(url):
    driver = setup_driver(headless=True)
    try:
        driver.get("https://snapinsta.to/en2")

        hook_js = r"""
        (function() {
          if (window.__snapinsta_cap && window.__snapinsta_cap.active) return;
          window.__snapinsta_cap = { active: true, events: [] };
          const target = '/api/ajaxSearch';
          function push(evt) { try { window.__snapinsta_cap.events.push(evt); } catch(e) {} }

          const of = window.fetch;
          if (of) {
            window.fetch = async function(...args) {
              const res = await of.apply(this, args);
              try {
                const reqUrl = (args && args[0] && args[0].toString()) || '';
                if (reqUrl.includes(target)) {
                  const txt = await res.clone().text();
                  push({kind: 'fetch', url: reqUrl, responseText: txt, status: res.status});
                }
              } catch(e) {}
              return res;
            };
          }

          const XO = XMLHttpRequest.prototype.open;
          const XS = XMLHttpRequest.prototype.send;
          XMLHttpRequest.prototype.open = function(method, reqUrl) {
            this.__snap_url = reqUrl;
            return XO.apply(this, arguments);
          };
          XMLHttpRequest.prototype.send = function(body) {
            this.addEventListener('load', function() {
              try {
                const reqUrl = this.__snap_url || '';
                if (reqUrl.includes(target)) {
                  push({kind: 'xhr', url: reqUrl, responseText: this.responseText, status: this.status});
                }
              } catch(e) {}
            });
            return XS.apply(this, arguments);
          };
        })();
        """
        driver.execute_script(hook_js)

        input_box = WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.CSS_SELECTOR, "input#s_input[name='q']")))
        input_box.clear()
        input_box.send_keys(url)

        button = WebDriverWait(driver, 15).until(EC.element_to_be_clickable((By.XPATH, "//button[contains(@onclick,'ksearchvideo') and contains(.,'Download')]")))
        driver.execute_script("arguments[0].click();", button)

        def got_ajax_response(drv):
            try:
                events = drv.execute_script("return (window.__snapinsta_cap && window.__snapinsta_cap.events) || []")
                for event in events:
                    if event.get("responseText"):
                        return event
                return False
            except Exception:
                return False

        event = WebDriverWait(driver, 60).until(got_ajax_response)
        decoded_html = _snapinsta_decode_obfuscated_js(event.get("responseText") or "")
        post_data = _snapinsta_parse_decoded_html(decoded_html)

        if not post_data:
            raise Exception("SnapInsta decoded response returned no usable media")

        try:
            metadata = fetch_instagram_og_metadata(url)
        except Exception as e:
            print(f"⚠️ SnapInsta metadata enrich error: {e}")
            metadata = {}

        return {
            "postData": post_data,
            "username": metadata.get("username", ""),
            "profilePic": "",
            "caption": metadata.get("caption", ""),
            "hashtags": metadata.get("hashtags", []),
        }
    except Exception as e:
        print(f"⚠️ SnapInsta error: {e}")
        raise Exception(str(e))
    finally:
        try:
            driver.quit()
        except Exception:
            pass

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
        user_agents = [
            "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:150.0) Gecko/20100101 Firefox/150.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:145.0) Gecko/20100101 Firefox/145.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:144.0) Gecko/20100101 Firefox/144.0",
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        ]
        csrf_chars = string.ascii_letters + string.digits
        csrf_token = "".join(random.choice(csrf_chars) for _ in range(40))
        headers = {
            "User-Agent": random.choice(user_agents),
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://grabgram.io/en",
            "Content-Type": "application/json",
            "X-CSRF-TOKEN": csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://grabgram.io",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
        }

        response = requests.post(
            "https://grabgram.io/api/fetch/instagram",
            headers=headers,
            json={"url": insta_url, "tool": "video"},
            timeout=45,
        )
        response.raise_for_status()
        data = response.json()

        if not data.get("ok"):
            raise Exception(f"GrabGram returned error: {data}")

        result = data.get("data") or {}
        items = result.get("items") or []
        if not items:
            raise Exception("GrabGram returned no media items")

        post_data = []
        for item in items:
            downloads = item.get("downloads") or []
            if not downloads:
                continue

            item_kind = (item.get("kind") or "").lower()
            preferred = None
            for download in downloads:
                kind = (download.get("kind") or "").lower()
                ext = (download.get("ext") or "").lower()
                if item_kind == "video" and (kind == "video" or ext == "mp4"):
                    preferred = download
                    break

            if preferred is None:
                preferred = downloads[0]

            media_url = preferred.get("url") or ""
            if not media_url:
                continue

            kind = (preferred.get("kind") or item_kind).lower()
            ext = (preferred.get("ext") or "").lower()
            is_video = kind == "video" or ext == "mp4"
            post_data.append({
                "type": "GraphVideo" if is_video else "GraphImage",
                "thumbnail": item.get("preview") or media_url,
                "link": media_url,
            })

        if not post_data:
            raise Exception("GrabGram returned no usable links")

        user = result.get("user") or {}

        caption = result.get("caption", "") or ""

        return {
            "postData": post_data,
            "username": user.get("username", "") or "",
            "profilePic": (
                user.get("profile_pic_url_hd")
                or user.get("profile_pic_url_sd")
                or ""
            ),
            "caption": caption,
            "hashtags": _extract_hashtags(caption),
        }
    except Exception as e:
        print(f"⚠️ GrabGram error: {e}")
        raise Exception(str(e))


class _GlobalSourceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []
        self.in_item = False
        self.item_div_depth = 0
        self.current = None
        self.in_link = False
        self.link_text_parts = []
        self.current_link = None
        self.in_option = False
        self.option_text_parts = []
        self.current_option = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        classes = set((attrs_dict.get("class", "") or "").split())

        if tag == "div":
            if not self.in_item and "download-items" in classes:
                self.in_item = True
                self.item_div_depth = 1
                self.current = {
                    "thumb": "",
                    "has_video_icon": False,
                    "anchors": [],
                    "options": [],
                }
            elif self.in_item:
                self.item_div_depth += 1
            return

        if not self.in_item:
            return

        if tag == "img" and not self.current.get("thumb"):
            src = attrs_dict.get("src", "").strip()
            if src:
                self.current["thumb"] = src
            return

        if tag == "i":
            if "icon-dlvideo" in classes:
                self.current["has_video_icon"] = True
            return

        if tag == "a":
            href = attrs_dict.get("href", "").strip()
            if href:
                self.in_link = True
                self.link_text_parts = []
                self.current_link = {
                    "href": href,
                    "title": (attrs_dict.get("title") or "").strip(),
                }
            return

        if tag == "option":
            value = attrs_dict.get("value", "").strip()
            if value:
                self.in_option = True
                self.option_text_parts = []
                self.current_option = {"value": value, "label": ""}

    def handle_data(self, data):
        if self.in_link:
            self.link_text_parts.append(data)
        if self.in_option:
            self.option_text_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.in_link:
            text = " ".join(part.strip() for part in self.link_text_parts).strip()
            self.current_link["text"] = text
            self.current["anchors"].append(self.current_link)
            self.in_link = False
            self.link_text_parts = []
            self.current_link = None
            return

        if tag == "option" and self.in_option:
            label = " ".join(part.strip() for part in self.option_text_parts).strip()
            self.current_option["label"] = label
            self.current["options"].append(self.current_option)
            self.in_option = False
            self.option_text_parts = []
            self.current_option = None
            return

        if tag == "div" and self.in_item:
            self.item_div_depth -= 1
            if self.item_div_depth <= 0:
                self.items.append(self.current)
                self.in_item = False
                self.item_div_depth = 0
                self.current = None


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=30))
def fetch_instagram_globalsource(insta_url: str, use_tor: bool = False) -> Dict[str, Any]:
    """Fetch Instagram media via globalsource.uk.com using curl, with Tor fallback on rate-limit."""
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    ]
    base_url = "https://globalsource.uk.com/"

    def _norm_url(raw: str) -> str:
        if not raw:
            return ""
        return urljoin(base_url, html.unescape(raw.strip()))

    def _pick_link(item: Dict[str, Any], want_video: bool) -> str:
        anchors = item.get("anchors", []) or []
        options = item.get("options", []) or []

        for anchor in anchors:
            combined = (
                f"{anchor.get('title', '')} {anchor.get('text', '')}"
            ).strip().lower()
            href = _norm_url(anchor.get("href", ""))
            if want_video and ("video" in combined or ".mp4" in href.lower()):
                return href
            if (not want_video) and ("image" in combined or "photo" in combined):
                return href

        if not want_video and options:
            return _norm_url(options[0].get("value", ""))

        for anchor in anchors:
            combined = (
                f"{anchor.get('title', '')} {anchor.get('text', '')}"
            ).strip().lower()
            if "thumbnail" in combined:
                continue
            href = _norm_url(anchor.get("href", ""))
            if href:
                return href

        if anchors:
            return _norm_url(anchors[0].get("href", ""))
        return ""

    try:
        ua = random.choice(user_agents)
        curl_cmd = [
            "curl",
            "--silent",
            "--show-error",
            "--location",
            "--max-time",
            "45",
            "https://globalsource.uk.com/action.php",
            "-X",
            "POST",
            "-H",
            "Origin: https://globalsource.uk.com",
            "-H",
            "Referer: https://globalsource.uk.com/",
            "-H",
            f"User-Agent: {ua}",
            "-F",
            f"url={insta_url}",
            "-F",
            "action=post",
        ]
        if use_tor:
            curl_cmd[6:6] = ["--socks5-hostname", "127.0.0.1:9050"]

        result = subprocess.run(curl_cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise Exception(f"globalsource curl failed: {result.stderr.strip()}")

        html_text = (result.stdout or "").strip()
        if not html_text:
            raise Exception("globalsource response empty")

        parser = _GlobalSourceParser()
        parser.feed(html_text)

        post_data = []
        for item in parser.items:
            anchors = item.get("anchors", []) or []
            is_video = bool(item.get("has_video_icon"))
            media_link = _pick_link(item, want_video=is_video)
            if not media_link and not is_video:
                media_link = _pick_link(item, want_video=False)

            if not media_link:
                continue

            if not is_video and ".mp4" in media_link.lower():
                is_video = True

            thumb_link = ""
            for anchor in anchors:
                combined = (
                    f"{anchor.get('title', '')} {anchor.get('text', '')}"
                ).strip().lower()
                if "thumbnail" in combined or "cover" in combined:
                    thumb_link = _norm_url(anchor.get("href", ""))
                    break

            base_thumb = _norm_url(item.get("thumb", ""))
            final_thumb = thumb_link or base_thumb
            if not final_thumb and not is_video:
                final_thumb = media_link

            post_data.append({
                "type": "GraphVideo" if is_video else "GraphImage",
                "thumbnail": final_thumb,
                "link": media_link,
            })

        if not post_data:
            raise Exception("globalsource returned no downloadable items")

        return {
            "postData": post_data,
            "username": "",
            "profilePic": "",
            "caption": "",
        }
    except Exception as e:
        print(f"⚠️ GlobalSource error: {e}")
        error_message = str(e).lower()
        blocked_patterns = (
            "429",
            "403",
            "too many",
            "rate limit",
            "cloudflare",
            "challenge",
            "captcha",
            "access denied",
            "timed out",
            "connection reset",
            "proxy connect aborted",
            "empty reply",
        )

        if any(p in error_message for p in blocked_patterns):
            if not use_tor:
                print("⚠️ GlobalSource blocked/rate-limited. Switching to Tor...")
                change_tor_ip()
                return fetch_instagram_globalsource(insta_url, use_tor=True)

            print("⚠️ GlobalSource still blocked on Tor. Rotating Tor IP...")
            change_tor_ip()
            raise Exception("GlobalSource still blocked after Tor retry")

        raise Exception(str(e))

DEVICE_TYPE_IOS = 1
DEVICE_TYPE_ANDROID = 2
DEVICE_TYPE_ANALYTICS_COLUMNS = {
    DEVICE_TYPE_IOS: "ios_requests",
    DEVICE_TYPE_ANDROID: "android_requests",
}
ANDROID_DOWNLOADGRAM_FIRST_SETTING = "ANDROID_DOWNLOADGRAM_FIRST"


def _validate_device_type(device_type: Optional[int]) -> Optional[int]:
    if device_type is None:
        return None
    if device_type not in DEVICE_TYPE_ANALYTICS_COLUMNS:
        raise ValueError("deviceType must be 1 (iOS) or 2 (Android)")
    return device_type


def _android_downloadgram_first_enabled() -> bool:
    value = get_setting(
        ANDROID_DOWNLOADGRAM_FIRST_SETTING,
        "false",
        encrypted=False,
    )
    return value.strip().lower() in {"1", "true", "yes", "on"}


def update_download_history(device_id: str, status: bool, device_type: Optional[int] = None):
    """
    status = "success" or "failure"
    """
    conn = get_connection()
    if not conn:
        print("⚠️ Skipping download history update: DB connection unavailable")
        return

    cursor = None
    try:
        cursor = conn.cursor()
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
                        device_type = COALESCE(%s, device_type),
                        updated_at = %s
                    WHERE device_unique_id = %s
                """
            else:
                query = """
                    UPDATE insta_download_history
                    SET backend_failure_count = backend_failure_count + 1,
                        frontend_failure_count = frontend_failure_count + 1,
                        device_type = COALESCE(%s, device_type),
                        updated_at = %s
                    WHERE device_unique_id = %s
                """
            cursor.execute(query, (device_type, now, device_id))
        else:
            # Insert new
            if status == True:
                query = """
                    INSERT INTO insta_download_history
                    (device_unique_id, device_type, backend_success_count, backend_failure_count, frontend_success_count, frontend_failure_count, created_at, updated_at)
                    VALUES (%s, %s, 1, 0, 0, 1, %s, %s)
                """
            else:
                query = """
                    INSERT INTO insta_download_history
                    (device_unique_id, device_type, backend_success_count, backend_failure_count, frontend_success_count, frontend_failure_count, created_at, updated_at)
                    VALUES (%s, %s, 0, 1, 0, 1, %s, %s)
                """
            cursor.execute(query, (device_id, device_type, now, now))

        conn.commit()

    except Error as e:
        print("DB Error:", e)

    finally:
        if cursor:
            cursor.close()
        conn.close()

# ✅ Function to log day-wise analytics in insta_analytics table only
def _ensure_analytics_day(cursor, today: str) -> None:
    cursor.execute("SELECT id FROM insta_analytics WHERE request_date = %s", (today,))
    if cursor.fetchone():
        return
    cursor.execute("""
        INSERT INTO insta_analytics (
            request_date,
            total_requests,
            total_success,
            total_failure
        )
        VALUES (%s, 0, 0, 0)
    """, (today,))


def log_platform_request(device_type: Optional[int]) -> None:
    """Count a download request once, independent of provider fallback attempts."""
    column = DEVICE_TYPE_ANALYTICS_COLUMNS.get(device_type)
    if not column:
        return

    conn = get_connection()
    if not conn:
        print("⚠️ Skipping platform analytics update: DB connection unavailable")
        return

    cursor = None
    try:
        cursor = conn.cursor()
        _ensure_analytics_platform_columns(cursor)
        today = datetime.now().strftime('%Y-%m-%d')
        _ensure_analytics_day(cursor, today)
        cursor.execute(
            f"UPDATE insta_analytics SET `{column}` = `{column}` + 1 WHERE request_date = %s",
            (today,),
        )
        conn.commit()
    except Error as e:
        print("Platform analytics DB Error:", e)
    finally:
        if cursor:
            cursor.close()
        conn.close()


def log_analytics(fallback_method: str, status: str, count_total: bool = True):
    conn = get_connection()
    if not conn:
        print("⚠️ Skipping analytics update: DB connection unavailable")
        return

    cursor = None
    try:
        cursor = conn.cursor()
        today = datetime.now().strftime('%Y-%m-%d')
        analytics_prefix = _analytics_column_prefix(fallback_method)
        success_column = f"{analytics_prefix}_success"
        failure_column = f"{analytics_prefix}_failure"
        _ensure_analytics_service_columns(cursor, success_column, failure_column)

        _ensure_analytics_day(cursor, today)

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

        service_column = success_column if status == "success" else failure_column
        cursor.execute(
            f"UPDATE insta_analytics SET `{service_column}` = `{service_column}` + 1 WHERE request_date = %s",
            (today,),
        )

        conn.commit()
    except Error as e:
        print("Analytics DB Error:", e)
    finally:
        if cursor:
            cursor.close()
        conn.close()

def _analytics_column_prefix(service_name: str) -> str:
    prefix = re.sub(r"[^0-9a-zA-Z_]+", "_", service_name or "unknown").strip("_").lower()
    return prefix or "unknown"

def _ensure_analytics_service_columns(cursor, success_column: str, failure_column: str) -> None:
    for column in (success_column, failure_column):
        cursor.execute("SHOW COLUMNS FROM insta_analytics LIKE %s", (column,))
        if cursor.fetchone():
            continue
        cursor.execute(f"ALTER TABLE insta_analytics ADD COLUMN `{column}` INT NOT NULL DEFAULT 0")


def _ensure_analytics_platform_columns(cursor) -> None:
    for column in DEVICE_TYPE_ANALYTICS_COLUMNS.values():
        cursor.execute("SHOW COLUMNS FROM insta_analytics LIKE %s", (column,))
        if cursor.fetchone():
            continue
        cursor.execute(f"ALTER TABLE insta_analytics ADD COLUMN `{column}` INT NOT NULL DEFAULT 0")

# ✅ Function to update frontend success count
def update_frontend_success(device_id: str, device_type: Optional[int] = None):
    conn = get_connection()
    if not conn:
        print("⚠️ Skipping frontend success update: DB connection unavailable")
        return

    cursor = None
    try:
        cursor = conn.cursor()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        cursor.execute("SELECT id FROM insta_download_history WHERE device_unique_id = %s", (device_id,))
        row = cursor.fetchone()

        if row:
            # Record exists → update
            query = """
                UPDATE insta_download_history
                SET frontend_success_count = frontend_success_count + 1,
                    device_type = COALESCE(%s, device_type),
                    updated_at = %s
                WHERE device_unique_id = %s
            """
            cursor.execute(query, (device_type, now, device_id))
        else:
            # Insert new row
            query = """
                INSERT INTO insta_download_history
                (device_unique_id, device_type, backend_success_count, backend_failure_count, frontend_success_count, frontend_failure_count, created_at, updated_at)
                VALUES (%s, %s, 0, 0, 1, 0, %s, %s)
            """
            cursor.execute(query, (device_id, device_type, now, now))

        conn.commit()

    except Error as e:
        print("DB Error (frontend_success):", e)

    finally:
        if cursor:
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
        ensure_apify_token_encryption(conn)
        with conn.cursor(dictionary=True, buffered=True) as cur:
            cur.execute("SELECT token, token_encrypted FROM apify_keys WHERE is_active=1 AND is_disabled=0 LIMIT 1")
            r = cur.fetchone()
            if r:
                return decrypt_apify_key_row(r)["token"]
            cur.execute("SELECT token, token_encrypted FROM apify_keys WHERE is_disabled=0 ORDER BY (max_amount_limit - current_balance) DESC LIMIT 1")
            r = cur.fetchone()
            return decrypt_apify_key_row(r)["token"] if r else None
    finally:
        conn.close()

# ✅ Function to fetch Instagram media via Apify Instagram Post Scraper
def fetch_apify_instagram_post(url: str) -> dict:
    # Read Apify token from DB first, fallback to environment variable
    token = get_active_apify_key() or get_setting("APIFY_TOKEN", os.getenv("APIFY_TOKEN", ""))
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

# ---------------------------------------------------------
# CURL OVER TOR (real browser-like request)
# ---------------------------------------------------------
def tor_curl_get(url: str) -> dict:
    for attempt in range(5):

        session_id = random.randint(100000, 999999)

        cmd = [
            "curl",
            "--proxy", f"socks5h://{session_id}@127.0.0.1:9050",
            url,
            "-H", "User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
            "-H", "Accept: */*",
            "-H", "Accept-Language: en-US,en;q=0.9",
            "-H", "Referer: https://www.instagram.com/",
            "--compressed",
            "--silent",
            "--max-time", "45",
            "--connect-timeout", "15"
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if not result.stdout:
            change_tor_ip()
            continue

        text = result.stdout

        if "Please wait a few minutes" in text or '"require_login":true' in text:
            print(f"Blocked on attempt {attempt+1}, rotating Tor")
            change_tor_ip()
            continue

        try:
            return json.loads(text)
        except Exception:
            change_tor_ip()

    raise Exception("All Tor circuits blocked")

def requests_get_json(url: str) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/",
        "X-Requested-With": "XMLHttpRequest",
    }

    for attempt in range(5):
        session = requests.Session()
        try:
            response = session.get(url, headers=headers, timeout=45)
            text = response.text or ""

            if (
                response.status_code in (401, 403, 429)
                or "Please wait a few minutes" in text
                or '"require_login":true' in text
            ):
                print(f"Blocked on requests attempt {attempt + 1}")
                continue

            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"⚠️ GraphQL requests attempt {attempt + 1} failed: {e}")
        finally:
            session.close()

    raise Exception("All GraphQL requests blocked")

def _is_blocked_instagram_response(data: Dict[str, Any]) -> bool:
    if not isinstance(data, dict):
        return True

    if data.get("error"):
        return True

    if data.get("require_login") is True or data.get("login_required") is True:
        return True

    status = str(data.get("status", "")).lower()
    message = str(data.get("message") or data.get("error_message") or "").lower()
    if status in {"fail", "failed", "error"}:
        return True

    blocked_terms = (
        "please wait a few minutes",
        "login",
        "checkpoint",
        "challenge",
        "blocked",
        "rate limit",
        "too many",
    )
    return any(term in message for term in blocked_terms)

def fetch_graphql_proxy_api(graphql_url: str) -> dict:
    api_url = "http://122.170.6.139/insta.php"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Cache-Control": "no-cache",
    }

    last_error = None
    for attempt in range(2):
        try:
            response = requests.get(
                api_url,
                params={"url": graphql_url},
                headers=headers,
                timeout=60,
            )
            response.raise_for_status()

            try:
                data = response.json()
            except Exception:
                text = response.text or ""
                i1, i2 = text.find("{"), text.rfind("}")
                if i1 == -1 or i2 <= i1:
                    raise
                data = json.loads(text[i1 : i2 + 1])

            if _is_blocked_instagram_response(data):
                raise ValueError(f"Proxy returned blocked/error response: {str(data)[:300]}")

            if not data.get("data", {}).get("xdt_shortcode_media"):
                raise ValueError("Proxy response missing data.xdt_shortcode_media")

            return data
        except Exception as e:
            last_error = e
            print(f"⚠️ GraphQL proxy attempt {attempt + 1} failed: {e}")
            time.sleep(random.uniform(1.0, 2.0))

    raise Exception(f"GraphQL proxy failed: {last_error}")


# ---------------------------------------------------------
# MAIN FUNCTION
# ---------------------------------------------------------
def fetch_instagram_instagraphql(insta_url: str) -> Dict[str, Any]:
    """
    Fast Instagram extractor using:
        indown → GraphQL URL
        insta.php proxy API → fetch JSON
        parse media
    """

    try:
        session = get_tor_session()
        INDOWN_API = "https://indown.ai/api/get-url"

        # ---------------- STEP 1: GET GRAPHQL URL ----------------
        print("🌐 Requesting GraphQL URL via Tor...")

        payload = {"l": insta_url}

        headers = {
            "Origin": "https://indown.ai",
            "Referer": "https://indown.ai/en/private",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": "Mozilla/5.0",
        }

        r = session.post(INDOWN_API, data=payload, headers=headers, timeout=60)

        if r.status_code != 200:
            change_tor_ip()
            raise Exception(f"indown.ai HTTP {r.status_code}")

        data = r.json()

        if data.get("status") != "ok":
            change_tor_ip()
            raise Exception("indown.ai failed")

        graphql_url = data.get("data")
        print(f"✅ GraphQL URL obtained: {graphql_url}")

        # ---------------- STEP 2: FETCH GRAPHQL ----------------
        print("📡 Fetching GraphQL via insta.php proxy API...")
        graphql_data = fetch_graphql_proxy_api(graphql_url)

        # ---------------- STEP 3: PARSE MEDIA ----------------
        media_info = graphql_data.get("data", {}).get("xdt_shortcode_media", {})

        if not media_info:
            raise Exception("Invalid GraphQL structure")

        post_data_list = []

        owner = media_info.get("owner", {})
        username = owner.get("username", "")
        profile_pic = owner.get("profile_pic_url", "")

        thumbnail = media_info.get("thumbnail_src", "")
        is_video = media_info.get("is_video", False)

        # ---- CAROUSEL ----
        sidecar = media_info.get("edge_sidecar_to_children", {}).get("edges", [])

        if sidecar:
            for edge in sidecar:
                node = edge.get("node", {})
                typename = node.get("__typename", "")

                if typename == "XDTGraphVideo":
                    post_data_list.append({
                        "type": "GraphVideo",
                        "thumbnail": node.get("display_url"),
                        "link": node.get("video_url")
                    })

                elif typename == "XDTGraphImage":
                    url = node.get("display_url")
                    post_data_list.append({
                        "type": "GraphImage",
                        "thumbnail": url,
                        "link": url
                    })

        # ---- SINGLE VIDEO ----
        elif is_video:
            post_data_list.append({
                "type": "GraphVideo",
                "thumbnail": thumbnail,
                "link": media_info.get("video_url")
            })

        # ---- SINGLE IMAGE ----
        else:
            display = media_info.get("display_url")
            post_data_list.append({
                "type": "GraphImage",
                "thumbnail": display,
                "link": display
            })

        if not post_data_list:
            raise Exception("No media found")

        # ---- CAPTION ----
        caption = ""
        edges = media_info.get("edge_media_to_caption", {}).get("edges", [])
        if edges:
            caption = edges[0].get("node", {}).get("text", "")

        print(f"✅ Extracted {len(post_data_list)} media items")

        return {
            "postData": post_data_list,
            "username": username,
            "profilePic": profile_pic,
            "caption": caption
        }

    except Exception as e:
        print("⚠️ InstagramGraphQL error:", e)
        raise Exception(f"InstagramGraphQL error: {str(e)}")
    
def fetch_instagram_saveclip(insta_url: str, headless: bool = True) -> Dict[str, Any]:
    """Fetch Instagram media via saveclip.app using Selenium with proxyorb proxy layer"""
    driver = setup_driver(headless=False)

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
        # Navigate to proxyorb instead of directly to saveclip
        print(f"🌐 Opening saveclip.app via proxyorb proxy browser...")
        driver.get("https://proxyorb.com/")

        # Wait for proxyorb page to fully load
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(2)

        # Paste saveclip.app URL into proxyorb's input field
        url_input = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'input[name="input"]'))
        )
        url_input.clear()
        url_input.send_keys("https://saveclip.app/en")
        print(f"📝 Pasted saveclip.app URL into proxyorb input")

        # Remove any ad iframes/overlays
        driver.execute_script("""
            document.querySelectorAll('iframe[id^="aswift"], iframe[src*="doubleclick"], iframe[src*="googleads"]').forEach(el => el.remove());
            document.querySelectorAll('[class*="adsbygoogle"], [id*="google_ads"]').forEach(el => el.remove());
        """)

        # Click "Start Proxy Browser" button
        start_btn = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'button[type="submit"]'))
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", start_btn)
        print(f"🖱️ Clicked Start Proxy Browser")

        # Wait for popup and click "Skip & Start Browsing"
        time.sleep(3)

        # Remove ad overlays again
        driver.execute_script("""
            document.querySelectorAll('iframe[id^="aswift"], iframe[src*="doubleclick"], iframe[src*="googleads"]').forEach(el => el.remove());
            document.querySelectorAll('[class*="adsbygoogle"], [id*="google_ads"]').forEach(el => el.remove());
        """)

        try:
            skip_btn = WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((
                    By.XPATH, "//button[contains(.,'Skip') and contains(.,'Start Browsing')]"
                ))
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", skip_btn)
            print(f"🖱️ Clicked Skip & Start Browsing")
        except Exception as skip_err:
            print(f"⚠️ Skip button not found with primary selector, trying alternative: {skip_err}")
            skip_btn = driver.find_element(By.XPATH, "//button[contains(span,'Skip')]")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", skip_btn)
            print(f"🖱️ Clicked Skip button via alternative selector")

        # Now we're inside saveclip.app via proxyorb
        print(f"⏳ Waiting for saveclip.app interface to load...")
        time.sleep(5)

        # Accept cookies if present
        try:
            WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((
                    By.CSS_SELECTOR,
                    "button#onetrust-accept-btn-handler, .fc-cta-consent, .ez-accept-all, button[class*='accept']"
                ))
            ).click()
            print(f"🍪 Accepted cookies")
        except Exception:
            pass

        # Find input field and enter Instagram URL
        input_field = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input#s_input, input[name='q']"))
        )
        input_field.clear()
        input_field.send_keys(insta_url)
        print(f"📝 Entered Instagram URL into saveclip input")
        time.sleep(0.5)

        # Click download button
        download_btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button.btn-default, button[onclick*='ksearchvideo']"))
        )
        download_btn.click()
        print(f"🖱️ Clicked download button")

        # Wait for download items to appear
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".download-items"))
        )
        print(f"✅ Download items loaded")

        time.sleep(2)  # Give extra time for all items to render

        # Get all download items
        download_items = driver.find_elements(By.CSS_SELECTOR, ".download-items")
        print(f"📦 Found {len(download_items)} download items")

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
                                break

                            # Fallback to any valid download link that's not a thumbnail
                            if dl and ("dl.snapcdn.app" in dl or ".mp4" in dl or ".jpg" in dl or ".jpeg" in dl or ".png" in dl):
                                download_link = dl
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
                    print(f"✅ Item {idx + 1}: {media_type}")
                else:
                    print(f"⚠️ No valid download link found for item {idx + 1}")

            except Exception as e:
                print(f"⚠️ Error processing item {idx + 1}: {e}")
                continue

        if not postData:
            raise Exception("⚠️ No download links found on saveclip.app via proxyorb")

        return {
            "postData": postData,
            "username": "",
            "profilePic": "",
            "caption": "",
        }

    except Exception as e:
        print(f"⚠️ SaveClip error (via proxyorb): {e}")
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
        r"^https?://(www\.)?threads\.(com|net)/@[A-Za-z0-9_.]+/post/[A-Za-z0-9_-]+$",
    ]

    channel_valid_patterns = [r"^https?://(www\.)?instagram\.com/[A-Za-z0-9_.]+$"]
    if any(re.match(p, clean_url) for p in channel_valid_patterns):
        return {"code": 200, "data": clean_url}

    if not any(re.match(p, clean_url) for p in valid_patterns):
        print(f"❌ Invalid Instagram URL: {clean_url}")
        return {"code": 400, "message": "The link you entered isn’t valid. Please verify it and try again."}

    return clean_url

def _instagram_service(
    name: str,
    handler: Callable[[], Dict[str, Any]],
    *,
    enabled: bool = True,
    analytics: str = "",
    condition: Callable[[], bool] = lambda: True,
    enrich_url: str = "",
    require_post_data: bool = False,
    final: bool = False,
    disabled_reason: str = "",
) -> Dict[str, Any]:
    return {
        "name": name,
        "handler": handler,
        "enabled": enabled,
        "analytics": analytics or name,
        "condition": condition,
        "enrich_url": enrich_url,
        "require_post_data": require_post_data,
        "final": final,
        "disabled_reason": disabled_reason,
    }

def _run_instagram_service(
    service: Dict[str, Any],
    device_id: str,
    context: str,
    device_type: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    name = service["name"]

    if not service.get("enabled", True):
        reason = service.get("disabled_reason") or "disabled"
        print(f"⏭️ {name} {context} skipped ({reason})")
        return None

    if not service["condition"]():
        print(f"⏭️ {name} {context} skipped (condition false)")
        return None

    analytics = service["analytics"]
    try:
        media_details = service["handler"]()
        if service.get("require_post_data") and (not media_details or not media_details.get("postData")):
            raise ValueError(f"{name} returned empty data")

        enrich_url = service.get("enrich_url")
        if enrich_url:
            media_details = enrich_instagram_metadata(media_details, enrich_url)

        update_download_history(device_id, True, device_type)
        log_analytics(analytics, "success")
        print(f"{name} {context} success")
        return {"code": 200, "data": media_details}
    except HTTPException:
        log_analytics(analytics, "failure", count_total=False)
        return None
    except Exception as e:
        print(f"⚠️ Error in {name} {context} fetch: {e}")
        log_analytics(analytics, "failure", count_total=False)
        return None


def _run_android_downloadgram_first(
    insta_url: str,
    device_id: str,
    context: str,
    device_type: Optional[int],
) -> Optional[Dict[str, Any]]:
    """Attempt DownloadGram once for Android without changing shared DB ordering."""
    service = _instagram_service(
        "downloadgram",
        lambda: fetch_instagram_downloadgram(insta_url),
        enrich_url=insta_url,
        require_post_data=True,
    )
    print(f"Android DownloadGram override enabled for {context}")
    return _run_instagram_service(service, device_id, context, device_type)

def _configured_instagram_services(context: str, services: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    configured_services = get_download_service_settings(context, services)
    return configured_services

def _prioritize_service(
    services: List[Dict[str, Any]],
    service_name: str = "",
) -> List[Dict[str, Any]]:
    if not service_name:
        return services

    priority = []
    remaining = []
    for service in services:
        if service["name"] == service_name and service.get("enabled", True):
            priority.append(service)
        else:
            remaining.append(service)

    return priority + remaining

def _run_instagram_services(
    services: List[Dict[str, Any]],
    device_id: str,
    context: str,
    preferred_first: str = "",
    device_type: Optional[int] = None,
    skip_service_names: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    configured_services = _configured_instagram_services(context, services)
    configured_services = _prioritize_service(configured_services, preferred_first)
    skipped_services = set(skip_service_names or [])
    if skipped_services:
        configured_services = [
            service for service in configured_services
            if service["name"] not in skipped_services
        ]
    active = [service["name"] for service in configured_services if service.get("enabled", True)]
    disabled = [service["name"] for service in configured_services if not service.get("enabled", True)]
    configured_order = [
        f"{service['name']}({service.get('sort_order', 'default')})"
        for service in configured_services
    ]
    print(f"Instagram {context} configured order: {configured_order}")
    print(f"Instagram {context} active services: {active}")
    print(f"Instagram {context} disabled services: {disabled}")

    for service in configured_services:
        response = _run_instagram_service(service, device_id, context, device_type)
        if response:
            return response

    return None

def _instagram_failure_response(device_id: str, device_type: Optional[int] = None) -> Dict[str, Any]:
    update_download_history(device_id, False, device_type)
    log_analytics("apify", "failure")
    return {"code": 200, "data": None, "message": "Media cannot be fetched. Please try again later."}

# ✅ FastAPI Endpoint to Download Instagram Media
async def download_media(
    instagramURL: str = Form(...),
    deviceId: str = Form(min_length=1),
    deviceType: Optional[int] = Form(default=None),
):

    device_type = _validate_device_type(deviceType)
    log_platform_request(device_type)
    print(f"🔍 Fetching actual media for URL: {instagramURL} | Device ID: {deviceId}")
    clean_url = normalize_instagram_url(instagramURL)
    if not isinstance(clean_url, dict) and not _is_threads_url(clean_url):
        post_type_check = check_instagram_privacy(instagramURL,use_tor=True)
        print(post_type_check)
        if post_type_check == "private":
            # log_analytics("privacy_check", "private")
            return {"code": 200, "data": None, "message": "Media cannot be fetched. Please try again later."}
    if isinstance(clean_url, dict):  # Error case
        if clean_url.get("code") == 200:
            print(f"🔍 media URL is profile URL: {clean_url}")
            profile_url = clean_url.get("data")
            android_downloadgram_first = (
                device_type == DEVICE_TYPE_ANDROID
                and _android_downloadgram_first_enabled()
            )
            if android_downloadgram_first:
                response = _run_android_downloadgram_first(
                    profile_url,
                    deviceId,
                    "profile",
                    device_type,
                )
                if response:
                    return response
            profile_services = [
                _instagram_service(
                    "saveclip",
                    lambda: fetch_instagram_saveclip(profile_url),
                    enabled=False,
                    disabled_reason="disabled in current profile workflow",
                ),
                _instagram_service(
                    "downloadgram",
                    lambda: fetch_instagram_downloadgram(profile_url),
                    enrich_url=profile_url,
                    require_post_data=True,
                ),
                _instagram_service(
                    "snapdownloader",
                    lambda: fetch_instagram_snapdownloader(profile_url),
                ),
                _instagram_service(
                    "snapinsta",
                    lambda: fetch_instagram_data(profile_url),
                    enrich_url=profile_url,
                ),
                _instagram_service(
                    "instagraphql",
                    lambda: fetch_instagram_instagraphql(profile_url),
                    enabled=False,
                    disabled_reason="disabled in current profile workflow",
                ),
                _instagram_service(
                    "instagram_oembed",
                    lambda: fetch_instagram_oembed_post(profile_url),
                    condition=lambda: _is_instagram_photo_post_url(profile_url),
                ),
                _instagram_service(
                    "yt_dlp",
                    lambda: fetch_instagram_ytdlp_video(profile_url),
                    enabled=False,
                    condition=lambda: _is_instagram_video_url(profile_url),
                    enrich_url=profile_url,
                    disabled_reason="disabled in current profile workflow",
                ),
                _instagram_service(
                    "globalsource",
                    lambda: fetch_instagram_globalsource(profile_url),
                    enabled=False,
                    disabled_reason="disabled in current profile workflow",
                ),
                _instagram_service(
                    "sss_profile",
                    lambda: fetch_sss_profile_posts(profile_url),
                    enabled=False,
                    disabled_reason="disabled in current profile workflow",
                ),
                _instagram_service(
                    "apify",
                    lambda: fetch_apify_instagram_post(profile_url),
                    require_post_data=True,
                    final=True,
                ),
            ]
            preferred_first = "instagram_oembed" if _is_instagram_photo_post_url(profile_url) else ""
            return _run_instagram_services(
                profile_services,
                deviceId,
                "profile",
                preferred_first=preferred_first,
                device_type=device_type,
                skip_service_names=["downloadgram"] if android_downloadgram_first else None,
            ) or _instagram_failure_response(deviceId, device_type)
        else:    
            return clean_url
    print(f"🔍 Fetching clean media for URL: {clean_url} | Device ID: {deviceId}")
    android_downloadgram_first = (
        device_type == DEVICE_TYPE_ANDROID
        and _android_downloadgram_first_enabled()
    )
    if android_downloadgram_first:
        response = _run_android_downloadgram_first(
            clean_url,
            deviceId,
            "post",
            device_type,
        )
        if response:
            return response
    post_services = [
        _instagram_service(
            "rapidapi",
            lambda: fetch_instagram_rapidapi_provider(clean_url),
            enrich_url=clean_url,
            require_post_data=True,
        ),
        _instagram_service(
            "downloadgram",
            lambda: fetch_instagram_downloadgram(clean_url),
            enrich_url=clean_url,
            require_post_data=True,
        ),
        _instagram_service(
            "snapdownloader",
            lambda: fetch_instagram_snapdownloader(clean_url),
        ),
        _instagram_service(
            "snapinsta",
            lambda: fetch_instagram_data(clean_url),
            enrich_url=clean_url,
        ),
        _instagram_service(
            "instagraphql",
            lambda: fetch_instagram_instagraphql(clean_url),
            enabled=False,
            enrich_url=clean_url,
            disabled_reason="disabled in current post workflow",
        ),
        _instagram_service(
            "instagram_oembed",
            lambda: fetch_instagram_oembed_post(clean_url),
            condition=lambda: _is_instagram_photo_post_url(clean_url),
        ),
        _instagram_service(
            "yt_dlp",
            lambda: fetch_instagram_ytdlp_video(clean_url),
            enabled=False,
            condition=lambda: _is_instagram_video_url(clean_url),
            enrich_url=clean_url,
            disabled_reason="disabled in current post workflow",
        ),
        _instagram_service(
            "instaloader",
            lambda: fetch_instagram_media(clean_url, use_tor=True),
            enabled=False,
            disabled_reason="disabled in current post workflow",
        ),
        _instagram_service(
            "saveclip",
            lambda: fetch_instagram_saveclip(clean_url),
            enabled=False,
            disabled_reason="disabled in current post workflow",
        ),
        _instagram_service(
            "globalsource",
            lambda: fetch_instagram_globalsource(clean_url),
            enabled=False,
            enrich_url=clean_url,
            disabled_reason="disabled in current post workflow",
        ),
        _instagram_service(
            "sssinstasave",
            lambda: fetch_instagram_sss(clean_url),
            enabled=False,
            disabled_reason="disabled in current post workflow",
        ),
        _instagram_service(
            "apify",
            lambda: fetch_apify_instagram_post(instagramURL),
            enrich_url=clean_url,
            require_post_data=True,
            final=True,
        ),
    ]
    preferred_first = "instagram_oembed" if _is_instagram_photo_post_url(clean_url) else ""
    return _run_instagram_services(
        post_services,
        deviceId,
        "post",
        preferred_first=preferred_first,
        device_type=device_type,
        skip_service_names=["downloadgram"] if android_downloadgram_first else None,
    ) or _instagram_failure_response(deviceId, device_type)

    
async def frontend_success(
    deviceId: str = Form(...),
    deviceType: Optional[int] = Form(default=None),
):
    try:
        device_type = _validate_device_type(deviceType)
        deviceId = deviceId.replace(" ", "")
        update_frontend_success(deviceId, device_type)
        return {"code": 200, "message": "Frontend success count updated"}

    except Exception as e:
        return {"code": 500, "data": None, "message": str(e)}

def _llm(prompt: str, system: str = "You are a helpful Instagram marketing expert.") -> str:
    """Call Google Gemini 3.1 Flash Lite and return the text response."""
    client = _get_gemini()
    response = client.models.generate_content(
        model='gemini-3.1-flash-lite',
        contents=[prompt],
        config=types.GenerateContentConfig(
            temperature=0.7,
            system_instruction=system
        ),
    )
    return response.text.strip()

def _gemini_text(response) -> str:
    text_parts = []
    for candidate in getattr(response, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            text = getattr(part, "text", None)
            if text:
                text_parts.append(text)
    if text_parts:
        return "".join(text_parts).strip()
    return (getattr(response, "text", "") or "").strip()

def _vision_gemini(prompt: str, tmp_path: str, orig_filename: str, system: str = "You are a helpful Instagram marketing expert.") -> str:
    """Call Google Gemini 2.5 Flash Video/Image Analysis and return the text response by uploading the entire media file."""
    client = _get_gemini()
    
    # Simple mime detection
    mime_type = "video/mp4" if orig_filename.lower().endswith((".mp4", ".mov", ".webm", ".avi")) else "image/jpeg"
    
    print(f"🔼 Uploading {mime_type} to Gemini Vision...")
    uploaded_file = client.files.upload(file=tmp_path, config={'mime_type': mime_type})
    
    # Wait for the file to be processed
    import time
    while getattr(uploaded_file.state, "name", str(uploaded_file.state)) == "PROCESSING":
        print(f"⏳ Waiting for Gemini to process {uploaded_file.name}...")
        time.sleep(2)
        uploaded_file = client.files.get(name=uploaded_file.name)
        
    if getattr(uploaded_file.state, "name", str(uploaded_file.state)) == "FAILED":
        raise ValueError("Gemini failed to process the media file.")
    
    try:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model='gemini-3.1-flash-lite',
                    contents=[
                        types.Content(role="user", parts=[
                            types.Part.from_uri(file_uri=uploaded_file.uri, mime_type=mime_type),
                            types.Part.from_text(text=prompt)
                        ])
                    ],
                    config=types.GenerateContentConfig(
                        temperature=0.7,
                        system_instruction=system
                    ),
                )
                return _gemini_text(response)
            except Exception as e:
                err_str = str(e)
                if ("503" in err_str or "UNAVAILABLE" in err_str or "429" in err_str) and attempt < 2:
                    print(f"⚠️ Gemini 503/429 Overload! Retrying {attempt+1}/3 in 3 seconds...")
                    time.sleep(3)
                else:
                    raise e
    finally:
        # Cleanup file from Gemini server storage immediately
        try:
            client.files.delete(name=uploaded_file.name)
        except Exception as e:
            print(f"⚠️ Failed to delete Gemini file: {e}")

def _query_groq(prompt: str, system: str = "You are a helpful Instagram marketing expert.") -> str:
    """Call Groq API (OpenAI compatible) to get text response."""
    key = get_setting("GROQ_API_KEY", os.getenv("GROQ_API_KEY", ""))
    if not key:
        raise ValueError("GROQ_API_KEY not configured")
    
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "llama-3.1-8b-instant",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 1024
    }
    
    response = requests.post(url, headers=headers, json=data, timeout=30)
    if response.status_code != 200:
        print(f"❌ Groq API Error ({response.status_code}): {response.text}")
    response.raise_for_status()
    res_json = response.json()
    return res_json['choices'][0]['message']['content'].strip()


async def trendy_captions(
    video_file: UploadFile = File(...),
    caption: str = Form(default=""),
    niche: str = Form(default=""),
):
    """Generate 3 trendy rewrite suggestions based on video transcript and original caption."""
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name
            content = await video_file.read()
            tmp.write(content)
        niche_hint = f" The niche is: {niche}." if niche else ""
        caption_hint = f" The original caption to draw inspiration from was: \"\"\"{caption}\"\"\"" if caption else ""
        
        orig_name = getattr(video_file, "filename", "")
        # Use full native multimodal analysis!
        prompt = (
            f"You are an Instagram marketing expert generating trendy captions.{niche_hint}{caption_hint}\n\n"
            "Watch this video carefully and listen to its audio (or look at the image). Based on exactly everything happening in it:\n"
            "Write ONE hyper-viral / trendy Instagram caption that fits the visual context.\n"
            "Include exactly 5 relevant, trending hashtags at the end.\n"
            "Return ONLY a JSON array with one string, nothing else.\n"
            "Example: [\"The single best viral caption here #tag1 #tag2...\"]"
        )
        raw = _vision_gemini(prompt, tmp_path, orig_name)
        
        # Extract JSON array robustly
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not match:
            raise ValueError("LLM did not return a JSON array")
        captions = json.loads(match.group())
        
        os.unlink(tmp_path)
        return {"code": 200, "data": {"captions": captions[:1]}}
    except Exception as e:
        print(f"⚠️ trendy_captions error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def trendy_hashtags(
    video_file: UploadFile = File(...),
    caption: str = Form(default=""),
    niche: str = Form(default=""),
):
    """Generate 30 relevant trending hashtags based on context."""
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name
            content = await video_file.read()
            tmp.write(content)
        niche_hint = f" The niche is: {niche}." if niche else ""
        caption_hint = f" The original caption to draw inspiration from was: \"\"\"{caption}\"\"\"" if caption else ""
        
        orig_name = getattr(video_file, "filename", "")
        prompt = (
            f"You are an Instagram hashtags expert.{niche_hint}{caption_hint}\n\n"
            "Watch this video carefully and listen to its audio (or look at the image). Based on exactly everything happening in it:\n"
            "Generate EXACTLY 30 highly relevant Instagram hashtags describing exactly what is seen and heard, split into 3 tiers of 10:\n"
            "- high_reach: massive popular hashtags (>1M posts)\n"
            "- mid_reach: medium popularity (100K-1M posts)\n"
            "- niche_reach: specific/niche hashtags describing exactly what is seen (<100K posts)\n\n"
            "Return ONLY a valid JSON object like:\n"
            "{\n"
            "  \"high_reach\": [\"#tag1\", ...],\n"
            "  \"mid_reach\":  [\"#tag1\", ...],\n"
            "  \"niche_reach\":[\"#tag1\", ...]\n"
            "}"
        )
        raw = _vision_gemini(prompt, tmp_path, orig_name)
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not match:
            raise ValueError("LLM did not return a JSON object")
        data = json.loads(match.group())
        os.unlink(tmp_path)
        return {"code": 200, "data": data}
    except Exception as e:
        print(f"⚠️ trendy_hashtags error: {e}")
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise HTTPException(status_code=500, detail=str(e))

async def groq_caption(
    text: str = Form(...),
):
    """Generate 10 trendy caption variants based on text input using Groq."""
    try:
        prompt = (
            f"You are an Instagram marketing expert generating trendy captions.\n"
            f"Based on this text: \"\"\"{text}\"\"\"\n\n"
            "Generate EXACTLY 10 different hyper-viral / trendy Instagram caption variants that fit this context.\n"
            "Each individual variant must include its own set of exactly 5 relevant, trending hashtags at the end.\n"
            "Return ONLY a valid JSON array of strings, nothing else.\n"
            "Example: [\"Caption 1 #tag1...\", \"Caption 2 #tag1...\", ...]"
        )
        raw = _query_groq(prompt)
        # Extract JSON array robustly
        import re
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not match:
            # Fallback if it didn't return JSON
            captions = [raw]
        else:
            captions = json.loads(match.group())
            
        return {"code": 200, "data": {"captions": captions}}
    except Exception as e:
        print(f"⚠️ groq_caption error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def groq_hashtags(
    text: str = Form(...),
):
    """Generate 10 groups of 5 hashtags each (50 total) based on text input using Groq."""
    try:
        prompt = (
            f"You are an Instagram hashtags expert.\n"
            f"Based on this context: \"\"\"{text}\"\"\"\n\n"
            "Generate EXACTLY 50 highly relevant Instagram hashtags describing the context.\n"
            "Return them as a JSON array of 10 strings, where each string contains exactly 5 hashtags separated by spaces.\n"
            "Return ONLY the JSON array, nothing else.\n"
            "Example: [\"#tag1 #tag2 #tag3 #tag4 #tag5\", \"#tag6 #tag7 #tag8 #tag9 #tag10\", ...]"
        )
        raw = _query_groq(prompt)
        import re
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not match:
            tags = [raw]
        else:
            tags = json.loads(match.group())
            
        return {"code": 200, "data": {"hashtags": tags}}
    except Exception as e:
        print(f"⚠️ groq_hashtags error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    

async def transcribe_video(
    video_file: UploadFile = File(...),
    target_language: str = Form(default="en"),
):
    """
    Transcribe a reel (uploaded as multipart file) using Gemini.
    The Flutter app uploads the locally saved .mp4 file.
    Returns the original transcript plus an English translation.
    """
    tmp_path = ""
    try:
        suffix = ".mp4"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
            content = await video_file.read()
            tmp.write(content)

        print(f"🎙️  Received video ({len(content)//1024} KB) — extracting transcript via Gemini Flash...")
        
        orig_name = getattr(video_file, "filename", "")
        prompt = (
            "Watch this video and listen to its audio carefully.\n"
            "If there is no audio track, no audible speech, or only music/sound effects with no spoken words, return no_audio true.\n"
            "1. If spoken words exist, transcribe them EXACTLY in their ORIGINAL language (e.g., Hindi, Arabic, etc.).\n"
            "2. Translate that transcript into clear, natural English.\n"
            "Return ONLY a JSON object:\n"
            "{\n"
            "  \"transcript\": \"the original language text\",\n"
            "  \"translated\": \"the english translation\",\n"
            "  \"language_code\": \"ISO 639-1 code of original language (e.g. 'hi', 'ar', 'es')\",\n"
            "  \"no_audio\": false,\n"
            "  \"message\": \"\"\n"
            "}"
        )
        raw = _vision_gemini(prompt, tmp_path, orig_name)
        
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not match:
            raise ValueError("Gemini response did not contain JSON")
            
        data = json.loads(match.group())
        transcript = data.get("transcript", "").strip()
        translated = data.get("translated", "").strip()
        detected_lang = data.get("language_code", "en").lower()
        no_audio = bool(data.get("no_audio")) or (not transcript and not translated)

        print(f"✅ Gemini Transcription done. Language: {detected_lang}, Length: {len(transcript)} chars")

        os.unlink(tmp_path)

        if no_audio:
            return {
                "code": 200,
                "message": "No audio found",
                "data": {
                    "transcript": "",
                    "translated": "",
                    "detected_language": "",
                    "no_audio": True,
                },
            }

        return {
            "code": 200,
            "data": {
                "transcript": transcript,
                "translated": translated,
                "detected_language": detected_lang,
                "no_audio": False,
            },
        }

    except Exception as e:
        print(f"⚠️ transcribe error: {e}")
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise HTTPException(status_code=500, detail=str(e))


async def extract_hook(
    video_file: UploadFile = File(...),
):
    """
    Identify the 'hook' in a reel — the most attention-grabbing opening moment.
    Accepts a multipart .mp4 upload from the Flutter app.
    Returns hook text + start/end timestamps in seconds.
    """
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name
            content = await video_file.read()
            tmp.write(content)

        print(f"🎙️  Received video ({len(content)//1024} KB) — extracting hook via Gemini API...")
        orig_name = getattr(video_file, "filename", "video.mp4")

        prompt = (
            "Watch this video carefully and listen to all audio.\n"
            "Identify the 'HOOK' — the specific words spoken in the first 0-10 seconds that are meant to grab attention.\n"
            "STRICT RULES:\n"
            "1. ONLY look at the first 10 seconds of the video.\n"
            "2. If there is no audio track, no audible speech, or only music/sound effects with no spoken words, return no_audio true.\n"
            "3. If spoken words exist, provide the EXACT verbatim text of what is said during that hook.\n"
            "4. Provide the precise start_time and end_time (in seconds) for when that text is spoken.\n"
            "Return ONLY a JSON object with these keys: 'hook_text', 'start_time', 'end_time', 'no_audio', 'message'.\n"
            "Example: {\"hook_text\": \"Stop scrolling! Do this instead...\", \"start_time\": 0.0, \"end_time\": 3.5, \"no_audio\": false, \"message\": \"\"}\n"
            "If no spoken audio exists, return {\"hook_text\": \"\", \"start_time\": \"\", \"end_time\": \"\", \"no_audio\": true, \"message\": \"No audio found\"}.\n"
            "If spoken audio exists but no clear hook exists, return empty hook text with no_audio false."
        )
        raw = _vision_gemini(prompt, tmp_path, orig_name)
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not match:
            raise ValueError("LLM did not return a JSON object")
        hook_data = json.loads(match.group())
        no_audio = bool(hook_data.get("no_audio"))
        if no_audio:
            hook_data.update({
                "hook_text": "",
                "start_time": "",
                "end_time": "",
                "no_audio": True,
                "message": "No audio found",
            })
        else:
            hook_data.setdefault("no_audio", False)
            hook_data.setdefault("message", "")

        os.unlink(tmp_path)

        response = {"code": 200, "data": hook_data}
        if hook_data.get("no_audio"):
            response["message"] = "No audio found"
        return response
    except Exception as e:
        print(f"⚠️ extract_hook error: {e}")
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise HTTPException(status_code=500, detail=str(e))
