import json
import unittest
from unittest.mock import Mock, patch

import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.endpoints.auth import router as auth_router
from app.core import client_crypto, rncryptor
from app.core.config import settings
from app.services import auth_service


BASE_URL = "https://auth.example.com"
KEY = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJ"
CLIENT_KEY = "client-key-shared-with-the-flutter-app"
PASSWORD = KEY[8:40]
USER = {
    "id": 6,
    "name": "Test User",
    "email": "user@example.com",
    "phone_number": None,
    "auth_key": "aa2d072d60848f890eff0f4909c08020",
    "email_verified": True,
}
SUCCESS = {"message": "Logged In successfully.", "code": 200, "data": {"user": USER}, "extra": None}


def encrypted_reply(body, status=200, password=PASSWORD):
    """What the auth service sends on success: the RNCryptor-encrypted JSON as the raw body."""
    return Mock(status_code=status, text=rncryptor.encrypt(json.dumps(body), password))


def plain_reply(body, status):
    """What the auth service sends on errors: plain JSON (or anything else)."""
    return Mock(status_code=status, text=body if isinstance(body, str) else json.dumps(body))


class ClientCryptoTests(unittest.TestCase):
    def test_round_trip_and_layout(self):
        with patch.object(settings, "client_encryption_key", CLIENT_KEY):
            encrypted = client_crypto.encrypt("hello")
            self.assertEqual(client_crypto.decrypt(encrypted), "hello")
        self.assertEqual(len(__import__("base64").b64decode(encrypted)), 12 + 16 + len("hello"))


class RNCryptorTests(unittest.TestCase):
    def test_round_trip(self):
        text = '{"token": "abc/+=ü"}'
        encrypted = rncryptor.encrypt(text, PASSWORD)
        self.assertTrue(encrypted.startswith("Aw"))  # version 3, password mode
        self.assertEqual(rncryptor.decrypt(encrypted, PASSWORD), text)

    def test_wrong_password_or_tampering_is_rejected(self):
        encrypted = rncryptor.encrypt("secret", PASSWORD)
        tampered = encrypted[:-6] + ("AAAAAA" if not encrypted.endswith("AAAAAA") else "BBBBBB")
        for name, value, password in (
            ("wrong password", encrypted, "x" * 32),
            ("tampered", tampered, PASSWORD),
            ("not base64", "%%%", PASSWORD),
            ("too short", "AwE=", PASSWORD),
        ):
            with self.subTest(name):
                with self.assertRaises(rncryptor.RNCryptorError):
                    rncryptor.decrypt(value, password)


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        for p in (
            patch.object(settings, "auth_service_base_url", BASE_URL + "/"),
            patch.object(settings, "auth_service_app", "insta-save"),
            patch.object(settings, "auth_service_timeout_seconds", 15.0),
            patch.object(settings, "auth_service_rncryptor_key", KEY),
            patch.object(settings, "auth_service_rncryptor_offset", 8),
            patch.object(settings, "auth_service_rncryptor_length", 32),
            patch.object(settings, "client_encryption_key", CLIENT_KEY),
        ):
            p.start()
            self.addCleanup(p.stop)
        request_patch = patch("app.services.auth_service.requests.request", return_value=encrypted_reply(SUCCESS))
        self.request = request_patch.start()
        self.addCleanup(request_patch.stop)
        # The DB side of login (accounts, sessions, history) is covered in test_sessions.py.
        session_patch = patch(
            "app.api.v1.endpoints.auth.session_service.complete_social_login",
            side_effect=lambda provider, body, *args: body,
        )
        self.complete_login = session_patch.start()
        self.addCleanup(session_patch.stop)

        app = FastAPI()
        app.include_router(auth_router)
        self.client = TestClient(app)

    def login(self, provider="google", headers=None, **fields):
        """Send an encrypted login like the Flutter app; return (status, decrypted body)."""
        body = client_crypto.encrypt(json.dumps({"token": "provider-token", **fields}))
        response = self.client.post(
            f"/auth/{provider}", content=body, headers={"Content-Type": "text/plain", **(headers or {})}
        )
        return response.status_code, self.decrypt(response)

    @staticmethod
    def decrypt(response):
        return json.loads(client_crypto.decrypt(response.text))

    def sent_payload(self):
        """Decrypt the body we sent to the auth service."""
        body = json.loads(self.request.call_args[1]["data"])  # a JSON string, like Laravel's Http::post
        return json.loads(rncryptor.decrypt(body, PASSWORD))


class SocialLoginTests(AuthTestCase):
    def test_google_success_is_encrypted_out_and_decrypted_back(self):
        status, body = self.login("google", deviceType="android")

        self.assertEqual(status, 200)
        self.assertEqual(body, SUCCESS)
        args, kwargs = self.request.call_args
        self.assertEqual(args, ("POST", BASE_URL + "/api/v2/social-login/google"))
        self.assertEqual(self.sent_payload(), {"token": "provider-token", "deviceType": "android"})
        self.assertNotIn("provider-token", kwargs["data"])
        self.assertEqual(kwargs["headers"]["App"], "insta-save")
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        self.assertEqual(kwargs["timeout"], 15.0)

    def test_apple_calls_apple_endpoint(self):
        self.assertEqual(self.login("apple")[0], 200)
        self.assertEqual(self.request.call_args[0][1], BASE_URL + "/api/v2/social-login/apple")
        self.assertEqual(self.sent_payload(), {"token": "provider-token"})

    def test_encrypted_reply_wrapped_in_json_quotes_is_accepted(self):
        reply = encrypted_reply(SUCCESS)
        reply.text = json.dumps(reply.text)
        self.request.return_value = reply
        self.assertEqual(self.login()[1], SUCCESS)

    def test_numeric_device_types_are_translated(self):
        for sent, expected in (("1", "ios"), ("2", "android"), ("IOS", "ios"), ("web", "web")):
            with self.subTest(sent):
                self.login(deviceType=sent)
                self.assertEqual(self.sent_payload()["deviceType"], expected)

    def test_unknown_device_type_is_not_sent(self):
        self.login(deviceType="toaster")
        self.assertNotIn("deviceType", self.sent_payload())

    def test_lang_header_is_forwarded_only_when_sent(self):
        self.login()
        self.assertNotIn("Lang", self.request.call_args[1]["headers"])
        self.login(headers={"Lang": "nl"})
        self.assertEqual(self.request.call_args[1]["headers"]["Lang"], "nl")

    def test_missing_token_is_a_validation_error_and_not_forwarded(self):
        for fields in ({"token": ""}, {"token": None}, {"token": 5}):
            with self.subTest(fields):
                status, body = self.login(**fields)
                self.assertEqual((status, body["code"]), (422, 422))
        self.request.assert_not_called()

    def test_encrypted_data_form_field_is_accepted(self):
        encrypted = client_crypto.encrypt(json.dumps({"token": "provider-token"}))
        response = self.client.post("/auth/google", data={"encrypted_data": encrypted})
        self.assertEqual(self.decrypt(response), SUCCESS)

    def test_body_sent_as_json_string_is_accepted(self):
        encrypted = client_crypto.encrypt(json.dumps({"token": "provider-token"}))
        response = self.client.post("/auth/google", json=encrypted)
        self.assertEqual(self.decrypt(response), SUCCESS)

    def test_bad_encrypted_requests_get_encrypted_400_and_are_not_forwarded(self):
        cases = {
            "plain form (old style)": dict(data={"token": "provider-token"}),
            "plain json": dict(json={"token": "provider-token"}),
            "empty": dict(content=b""),
            "garbage": dict(content=b"not encrypted"),
            "json list": dict(content=client_crypto.encrypt("[1]").encode()),
        }
        with patch.object(settings, "client_encryption_key", "another-key"):
            cases["wrong key"] = dict(content=client_crypto.encrypt(json.dumps({"token": "t"})).encode())
        for name, kwargs in cases.items():
            with self.subTest(name):
                response = self.client.post("/auth/google", **kwargs)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.decrypt(response)["message"], "Invalid encrypted request.")
        self.request.assert_not_called()

    def test_missing_client_key_returns_plain_500(self):
        with patch.object(settings, "client_encryption_key", ""):
            response = self.client.post("/auth/google", content=b"x")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["code"], 500)
        self.request.assert_not_called()

    def test_plain_json_4xx_errors_are_passed_through(self):
        for code, message in ((401, "Invalid token."), (403, "Verify email"), (422, "token is required"), (429, "Too Many Requests")):
            with self.subTest(code):
                self.request.return_value = plain_reply({"message": message, "code": code}, code)
                self.assertEqual(self.login(), (code, {"code": code, "data": None, "message": message}))

    def test_encrypted_4xx_error_is_passed_through(self):
        self.request.return_value = encrypted_reply({"message": "Invalid token.", "code": 401}, status=401)
        self.assertEqual(self.login()[1]["message"], "Invalid token.")

    def test_auth_service_failures_return_generic_500(self):
        cases = {
            "server error": plain_reply({"message": "Something went wrong. Please try again.", "code": 500}, 500),
            "html error page": plain_reply("<html>Bad gateway</html>", 502),
            "encrypted with another key": encrypted_reply(SUCCESS, password="y" * 32),
            "json list": plain_reply(["unexpected"], 200),
        }
        for name, reply in cases.items():
            with self.subTest(name):
                self.request.return_value = reply
                self.assertEqual(
                    self.login(),
                    (500, {"code": 500, "data": None, "message": "Something went wrong. Please try again."}),
                )

    def test_unreachable_auth_service_returns_500(self):
        self.request.side_effect = requests.ConnectTimeout()
        self.assertEqual(self.login()[0], 500)

    def test_missing_configuration_returns_500_without_calling_out(self):
        for setting in ("auth_service_base_url", "auth_service_app", "auth_service_rncryptor_key"):
            with self.subTest(setting), patch.object(settings, setting, ""):
                self.assertEqual(self.login()[0], 500)
        self.request.assert_not_called()


class GetUserTests(AuthTestCase):
    """auth_service.get_user, used by auto login to re-check an account's auth_key."""

    def test_valid_auth_key_is_sent_encrypted_and_returns_user(self):
        self.request.return_value = encrypted_reply({"message": "OK", "code": 200, "data": {"user": USER}})

        self.assertEqual(auth_service.get_user(USER["auth_key"]), USER)
        args, kwargs = self.request.call_args
        self.assertEqual(args, ("GET", BASE_URL + "/api/v2/user"))
        self.assertEqual(rncryptor.decrypt(kwargs["headers"]["Auth-Key"], PASSWORD), USER["auth_key"])
        self.assertEqual(kwargs["headers"]["App"], "insta-save")

    def test_rejected_auth_key_returns_none(self):
        self.request.return_value = plain_reply({"message": "Unauthorized", "code": 401}, 401)
        self.assertIsNone(auth_service.get_user("bad"))

    def test_auth_service_down_raises(self):
        self.request.side_effect = requests.ConnectionError()
        with self.assertRaises(auth_service.AuthServiceError):
            auth_service.get_user("x")


if __name__ == "__main__":
    unittest.main()
