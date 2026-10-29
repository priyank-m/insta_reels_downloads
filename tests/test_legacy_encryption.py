import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.core import client_crypto
from app.core.config import settings
from app.main import create_app


CLIENT_KEY = "client-key-shared-with-the-apps"
RESULT = {"code": 200, "data": {"ok": True}, "message": "done"}


class LegacyEncryptionTestCase(unittest.TestCase):
    def setUp(self):
        for p in (
            patch.object(settings, "client_encryption_key", CLIENT_KEY),
            patch.object(settings, "encryption_required", False),
            patch.object(settings, "ai_requires_subscription", False),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.download = AsyncMock(return_value=RESULT)
        self.seen_files = {}

        async def transcribe(video_file, target_language):
            self.seen_files["video_file"] = (video_file.filename, await video_file.read(), target_language)
            return RESULT

        for name, mock in (("download_media", self.download), ("transcribe_video", AsyncMock(side_effect=transcribe))):
            p = patch(f"app.services.instagram_service.{name}", new=mock)
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(create_app())

    @staticmethod
    def enc(fields):
        return client_crypto.encrypt(json.dumps(fields))

    @staticmethod
    def dec(response):
        return json.loads(client_crypto.decrypt(response.text))

    def required(self, value=True):
        p = patch.object(settings, "encryption_required", value)
        p.start()
        self.addCleanup(p.stop)


class OptionalModeTests(LegacyEncryptionTestCase):
    """ENCRYPTION_REQUIRED=0: old apps keep working, new apps may encrypt."""

    def test_plain_form_works_as_before(self):
        response = self.client.post("/download_media", data={"instagramURL": "https://i.com/p/1", "deviceId": "d", "deviceType": "2"})
        self.assertEqual(response.json(), RESULT)
        self.download.assert_awaited_once_with(instagramURL="https://i.com/p/1", deviceId="d", deviceType=2)

    def test_encrypted_text_body_is_decrypted_and_answer_encrypted(self):
        body = self.enc({"instagramURL": "https://i.com/p/2", "deviceId": "dev", "deviceType": 1})
        response = self.client.post("/download_media", content=body, headers={"Content-Type": "text/plain"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))
        self.assertEqual(self.dec(response), RESULT)
        self.download.assert_awaited_once_with(instagramURL="https://i.com/p/2", deviceId="dev", deviceType=1)

    def test_encrypted_body_sent_as_json_string_works(self):
        body = json.dumps(self.enc({"instagramURL": "u", "deviceId": "d"}))
        response = self.client.post("/download_media", content=body, headers={"Content-Type": "application/json"})
        self.assertEqual(self.dec(response), RESULT)

    def test_multipart_encrypted_fields_with_plain_file(self):
        response = self.client.post(
            "/transcribe",
            data={"encrypted_data": self.enc({"target_language": "hi"}), "target_language": "plain-loses"},
            files={"video_file": ("reel.mp4", b"\x00\x01video-bytes", "video/mp4")},
        )
        self.assertEqual(self.dec(response), RESULT)
        self.assertEqual(self.seen_files["video_file"], ("reel.mp4", b"\x00\x01video-bytes", "hi"))

    def test_urlencoded_encrypted_data_field_works(self):
        response = self.client.post("/download_media", data={"encrypted_data": self.enc({"instagramURL": "u", "deviceId": "d"})})
        self.assertEqual(self.dec(response), RESULT)

    def test_bad_ciphertext_is_encrypted_400_and_not_run(self):
        for kwargs in (
            dict(content=b"not-encrypted", headers={"Content-Type": "text/plain"}),
            dict(data={"encrypted_data": "garbage"}),
        ):
            with self.subTest(kwargs=kwargs):
                response = self.client.post("/download_media", **kwargs)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.dec(response)["message"], "Invalid encrypted request.")
        self.download.assert_not_awaited()

    def test_validation_still_applies_and_answer_is_encrypted(self):
        response = self.client.post("/download_media", content=self.enc({"deviceId": "d"}), headers={"Content-Type": "text/plain"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("detail", self.dec(response))  # FastAPI's validation error, encrypted
        response = self.client.post("/download_media", content=self.enc({"instagramURL": "u", "deviceId": "d", "deviceType": 9}),
                                    headers={"Content-Type": "text/plain"})
        self.assertEqual(response.status_code, 422)
        self.download.assert_not_awaited()

    def test_missing_key_with_encrypted_request_is_plain_500(self):
        body = self.enc({"instagramURL": "u", "deviceId": "d"})
        with patch.object(settings, "client_encryption_key", ""):
            response = self.client.post("/download_media", content=body, headers={"Content-Type": "text/plain"})
        self.assertEqual((response.status_code, response.json()["code"]), (500, 500))


class RequiredModeTests(LegacyEncryptionTestCase):
    """ENCRYPTION_REQUIRED=1: only encrypted requests."""

    def setUp(self):
        super().setUp()
        self.required()

    def test_plain_requests_are_rejected_for_every_old_api(self):
        for path in ("/download_media", "/frontend_success", "/transcribe", "/extract_hook", "/trendy_captions",
                     "/trendy_hashtags", "/groq_caption", "/groq_hashtags"):
            with self.subTest(path=path):
                response = self.client.post(path, data={"deviceId": "d", "text": "x"})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"code": 400, "data": None, "message": "Encrypted request required."})
        self.download.assert_not_awaited()

    def test_encrypted_requests_work(self):
        response = self.client.post("/download_media", content=self.enc({"instagramURL": "u", "deviceId": "d"}),
                                    headers={"Content-Type": "text/plain"})
        self.assertEqual(self.dec(response), RESULT)

    def test_other_apis_are_not_affected(self):
        response = self.client.get("/history")  # needs login, not encryption
        self.assertEqual(response.status_code, 401)
        response = self.client.get("/download_media")  # only POST is handled
        self.assertEqual(response.status_code, 405)


if __name__ == "__main__":
    unittest.main()
