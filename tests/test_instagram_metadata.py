import unittest
from unittest.mock import Mock, patch

from app.services import instagram_service


class InstagramMetadataTests(unittest.TestCase):
    def setUp(self):
        instagram_service._instagram_page_metadata_cache.clear()
        instagram_service._instagram_profile_image_cache.clear()
        instagram_service._instagram_oembed_metadata_cache.clear()

    @patch("app.services.instagram_service._is_instagram_photo_post_url", return_value=False)
    @patch("app.services.instagram_service.fetch_instagram_profile_picture")
    @patch("app.services.instagram_service.fetch_instagram_og_metadata")
    def test_downloadgram_style_result_is_enriched_without_changing_media(self, fetch_post, fetch_profile, is_photo):
        fetch_post.return_value = {
            "username": "creator.name",
            "caption": "Caption #one #two",
            "hashtags": ["#one", "#two"],
        }
        fetch_profile.return_value = "https://scontent.cdninstagram.com/profile.jpg"
        media = {
            "postData": [{"type": "GraphVideo", "link": "https://cdn.example/video.mp4"}],
            "username": "",
            "profilePic": "",
            "caption": "",
        }

        result = instagram_service.enrich_instagram_metadata(
            media,
            "https://www.instagram.com/p/SHORTCODE",
        )

        self.assertEqual(result["postData"], media["postData"])
        self.assertEqual(result["username"], "creator.name")
        self.assertEqual(result["caption"], "Caption #one #two")
        self.assertEqual(result["hashtags"], ["#one", "#two"])
        self.assertEqual(result["profilePic"], "https://scontent.cdninstagram.com/profile.jpg")

    @patch("app.services.instagram_service.fetch_instagram_profile_picture")
    @patch("app.services.instagram_service.fetch_instagram_og_metadata")
    @patch("app.services.instagram_service.fetch_instagram_oembed_metadata")
    @patch("app.services.instagram_service._is_instagram_oembed_metadata_enrichment_enabled", return_value=True)
    def test_photo_oembed_fills_missing_username_without_replacing_media(
        self, is_enabled, fetch_oembed, fetch_post, fetch_profile
    ):
        fetch_oembed.return_value = {
            "username": "oembed.creator",
            "caption": "Caption #one",
            "hashtags": ["#one"],
        }
        fetch_profile.return_value = ""
        media = {
            "postData": [{"type": "GraphImage", "link": "https://cdn.example/photo.jpg"}],
            "username": "",
            "profilePic": "",
            "caption": "",
        }

        result = instagram_service.enrich_instagram_metadata(
            media,
            "https://www.instagram.com/p/SHORTCODE",
        )

        self.assertEqual(result["postData"], media["postData"])
        self.assertEqual(result["username"], "oembed.creator")
        self.assertEqual(result["caption"], "Caption #one")
        self.assertEqual(result["hashtags"], ["#one"])
        fetch_post.assert_not_called()
        fetch_profile.assert_called_once_with("oembed.creator")

    @patch("app.services.instagram_service.fetch_instagram_og_metadata")
    @patch("app.services.instagram_service.fetch_instagram_oembed_metadata", side_effect=RuntimeError("unavailable"))
    @patch("app.services.instagram_service._is_instagram_oembed_metadata_enrichment_enabled", return_value=True)
    def test_oembed_failure_never_breaks_successful_media_result(self, is_enabled, fetch_oembed, fetch_post):
        fetch_post.return_value = {"username": "", "caption": "", "hashtags": []}
        media = {"postData": [{"type": "GraphImage", "link": "https://cdn.example/photo.jpg"}]}

        result = instagram_service.enrich_instagram_metadata(
            media,
            "https://www.instagram.com/p/SHORTCODE",
        )

        self.assertEqual(result["postData"], media["postData"])
        self.assertEqual(result["username"], "")
        self.assertEqual(result["profilePic"], "")
        self.assertEqual(result["caption"], "")

    @patch("app.services.instagram_service.fetch_instagram_profile_picture")
    @patch("app.services.instagram_service.fetch_instagram_og_metadata")
    @patch("app.services.instagram_service.fetch_instagram_oembed_metadata")
    @patch("app.services.instagram_service._is_instagram_oembed_metadata_enrichment_enabled", return_value=True)
    def test_video_oembed_enriches_missing_username_without_changing_provider_data(
        self, is_enabled, fetch_oembed, fetch_post, fetch_profile
    ):
        fetch_oembed.return_value = {
            "username": "video.creator",
            "caption": "oEmbed caption",
            "hashtags": ["#oembed"],
        }
        fetch_profile.return_value = "https://scontent.cdninstagram.com/profile.jpg"
        media = {
            "postData": [{"type": "GraphVideo", "link": "https://cdn.example/video.mp4"}],
            "username": "",
            "profilePic": "",
            "caption": "RapidAPI caption",
            "hashtags": ["#rapidapi"],
        }

        result = instagram_service.enrich_instagram_metadata(
            media,
            "https://www.instagram.com/p/VIDEO_SHORTCODE",
        )

        self.assertEqual(result["postData"], media["postData"])
        self.assertEqual(result["username"], "video.creator")
        self.assertEqual(result["profilePic"], "https://scontent.cdninstagram.com/profile.jpg")
        self.assertEqual(result["caption"], "RapidAPI caption")
        self.assertEqual(result["hashtags"], ["#rapidapi"])
        fetch_post.assert_not_called()

    @patch("app.services.instagram_service.fetch_instagram_oembed_metadata")
    @patch("app.services.instagram_service.fetch_instagram_og_metadata")
    def test_oembed_enrichment_is_disabled_by_default(self, fetch_post, fetch_oembed):
        fetch_post.return_value = {"username": "", "caption": "", "hashtags": []}
        media = {"postData": [{"type": "GraphVideo", "link": "https://cdn.example/video.mp4"}]}

        result = instagram_service.enrich_instagram_metadata(
            media,
            "https://www.instagram.com/p/VIDEO_SHORTCODE",
        )

        self.assertEqual(result["postData"], media["postData"])
        fetch_oembed.assert_not_called()

    @patch("app.services.instagram_service.requests.get")
    def test_oembed_metadata_reads_author_name_without_requiring_thumbnail(self, get):
        response = Mock()
        response.json.return_value = {
            "author_name": "oembed.creator",
            "title": "Caption #one #two",
        }
        get.return_value = response

        metadata = instagram_service.fetch_instagram_oembed_metadata(
            "https://www.instagram.com/p/SHORTCODE"
        )

        self.assertEqual(metadata["username"], "oembed.creator")
        self.assertEqual(metadata["caption"], "Caption")
        self.assertEqual(metadata["hashtags"], ["#one", "#two"])

    @patch("app.services.instagram_service.fetch_instagram_profile_picture")
    @patch("app.services.instagram_service.fetch_instagram_og_metadata")
    def test_existing_rapidapi_metadata_is_preserved_while_profile_picture_is_filled(self, fetch_post, fetch_profile):
        fetch_profile.return_value = "https://scontent.cdninstagram.com/profile.jpg"
        media = {
            "postData": [{"type": "GraphImage", "link": "https://cdn.example/image.jpg"}],
            "username": "rapid_user",
            "profilePic": "",
            "caption": "Provider caption",
            "hashtags": ["#provider"],
        }

        result = instagram_service.enrich_instagram_metadata(
            media,
            "https://www.instagram.com/p/SHORTCODE",
        )

        fetch_post.assert_not_called()
        fetch_profile.assert_called_once_with("rapid_user")
        self.assertEqual(result["caption"], "Provider caption")
        self.assertEqual(result["hashtags"], ["#provider"])
        self.assertEqual(result["profilePic"], "https://scontent.cdninstagram.com/profile.jpg")

    @patch("app.services.instagram_service._is_instagram_photo_post_url", return_value=False)
    @patch("app.services.instagram_service.fetch_instagram_profile_picture")
    @patch("app.services.instagram_service.fetch_instagram_og_metadata", side_effect=RuntimeError("blocked"))
    def test_metadata_failure_never_breaks_successful_media_result(self, fetch_post, fetch_profile, is_photo):
        media = {
            "postData": [{"type": "GraphImage", "link": "https://cdn.example/image.jpg"}],
        }

        result = instagram_service.enrich_instagram_metadata(
            media,
            "https://www.instagram.com/p/SHORTCODE",
        )

        self.assertEqual(result["postData"], media["postData"])
        self.assertEqual(result["username"], "")
        self.assertEqual(result["profilePic"], "")
        self.assertEqual(result["caption"], "")
        self.assertEqual(result["hashtags"], [])
        fetch_profile.assert_not_called()

    @patch("app.services.instagram_service.fetch_instagram_page_metadata")
    def test_profile_picture_requires_valid_username_and_safe_cdn_url(self, fetch_page):
        parser = Mock()
        parser.og_image = "https://attacker.example/profile.jpg"
        fetch_page.return_value = parser

        self.assertEqual(instagram_service.fetch_instagram_profile_picture("valid.user"), "")
        self.assertEqual(instagram_service.fetch_instagram_profile_picture("invalid/user"), "")
        self.assertEqual(fetch_page.call_count, 1)

    @patch("app.services.instagram_service.fetch_instagram_profile_picture", side_effect=RuntimeError("blocked"))
    def test_profile_picture_failure_never_breaks_successful_media_result(self, fetch_profile):
        media = {
            "postData": [{"type": "GraphImage", "link": "https://cdn.example/image.jpg"}],
            "username": "creator.name",
            "caption": "Provider caption",
            "hashtags": ["#provider"],
            "profilePic": "",
        }

        result = instagram_service.enrich_instagram_metadata(
            media,
            "https://www.instagram.com/p/SHORTCODE",
        )

        self.assertEqual(result["postData"], media["postData"])
        self.assertEqual(result["profilePic"], "")
        fetch_profile.assert_called_once_with("creator.name")

    @patch("app.services.instagram_service.requests.get")
    def test_profile_picture_lookup_is_cached(self, get):
        response = Mock()
        response.text = '<meta property="og:image" content="https://scontent.cdninstagram.com/profile.jpg">'
        get.return_value = response

        first = instagram_service.fetch_instagram_profile_picture("creator.name")
        second = instagram_service.fetch_instagram_profile_picture("creator.name")

        self.assertEqual(first, "https://scontent.cdninstagram.com/profile.jpg")
        self.assertEqual(second, first)
        self.assertEqual(get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
