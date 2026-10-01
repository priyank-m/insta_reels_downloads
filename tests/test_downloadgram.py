import base64
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from app.services.instagram_service import (
    DOWNLOADGRAM_API_URL,
    _parse_downloadgram_response,
    fetch_instagram_downloadgram,
)


def _download_url(original_url: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"url": original_url}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"//cdn.downloadgram.org/?{urlencode({'token': f'header.{payload}.signature'})}"


def _escaped_response(*links: str) -> str:
    markup = "".join(
        f'<div class="row"><img src="{link}"><a download href="{link}">DOWNLOAD</a></div>'
        for link in links
    )
    return "document['getElementById']('downloadhere')['innerHTML']='" + markup.replace(" ", r"\x20").replace('"', r"\x22") + "'"


class DownloadGramParserTests(unittest.TestCase):
    def test_parses_escaped_carousel_and_classifies_media(self):
        image_url = "https://scontent.cdninstagram.com/path/photo.jpg"
        video_url = "https://scontent.cdninstagram.com/path/reel.mp4"
        image_link = _download_url(image_url)
        video_link = _download_url(video_url)

        post_data = _parse_downloadgram_response(_escaped_response(image_link, video_link))

        self.assertEqual([item["type"] for item in post_data], ["GraphImage", "GraphVideo"])
        self.assertEqual([item["link"] for item in post_data], [image_url, video_url])
        self.assertEqual([item["thumbnail"] for item in post_data], [image_url, video_url])

    def test_rejects_non_media_tokens_non_downloadgram_links_and_duplicates(self):
        stream_url = "https://scontent.cdninstagram.com/path/photo.jpg"
        valid_link = _download_url(stream_url)
        unsafe_link = _download_url("https://attacker.example/video.mp4")
        response = _escaped_response(valid_link, valid_link, unsafe_link) + '<a href="javascript:alert(1)">bad</a>'

        post_data = _parse_downloadgram_response(response)

        self.assertEqual(len(post_data), 1)
        self.assertEqual(post_data[0]["link"], stream_url)

    @patch("app.services.instagram_service.requests.post")
    def test_posts_form_data_and_returns_project_schema(self, post):
        link = _download_url("https://scontent.cdninstagram.com/path/photo.jpg")
        response = Mock()
        response.text = _escaped_response(link)
        post.return_value = response

        result = fetch_instagram_downloadgram("https://www.instagram.com/p/SHORTCODE")

        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], DOWNLOADGRAM_API_URL)
        self.assertEqual(post.call_args.kwargs["data"], {
            "url": "https://www.instagram.com/p/SHORTCODE",
            "v": "3",
            "lang": "en",
        })
        response.raise_for_status.assert_called_once_with()
        self.assertEqual(result["postData"][0]["type"], "GraphImage")
        self.assertEqual(result["username"], "")
        self.assertEqual(result["profilePic"], "")
        self.assertEqual(result["caption"], "")

    @patch("app.services.instagram_service.requests.post")
    def test_raises_for_an_empty_or_unusable_response(self, post):
        response = Mock()
        response.text = '<a href="https://example.com/file.jpg">DOWNLOAD</a>'
        post.return_value = response

        with self.assertRaisesRegex(ValueError, "no usable media"):
            fetch_instagram_downloadgram("https://www.instagram.com/p/SHORTCODE")


if __name__ == "__main__":
    unittest.main()
