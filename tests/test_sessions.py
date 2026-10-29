"""Guest login, Google/Apple login, auto login, logout and history against a real MySQL/MariaDB.

Needs an empty throwaway database (tables are dropped and re-created):
    TEST_DB_HOST=127.0.0.1 TEST_DB_PORT=3399 TEST_DB_USER=... TEST_DB_PASSWORD=... TEST_DB_NAME=isave_test
Skipped when TEST_DB_NAME is not set. The auth service is faked.
"""
import json
import os
import unittest
from unittest.mock import Mock, patch

import mysql.connector
import requests
from fastapi.testclient import TestClient

from app.core import client_crypto, rncryptor
from app.core.config import settings
from app.main import create_app
from app.repositories import account_repository


DB = {
    "host": os.getenv("TEST_DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("TEST_DB_PORT", "3306")),
    "user": os.getenv("TEST_DB_USER", "root"),
    "password": os.getenv("TEST_DB_PASSWORD", ""),
    "database": os.getenv("TEST_DB_NAME", ""),
}
KEY = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJ"
PASSWORD = KEY[8:40]
CLIENT_KEY = "client-key-shared-with-the-apps"


def auth_user(user_id=6, auth_key="auth-key-6", name="Test User"):
    return {"id": user_id, "name": name, "email": f"user{user_id}@example.com", "auth_key": auth_key}


def encrypted_reply(body, status=200):
    return Mock(status_code=status, text=rncryptor.encrypt(json.dumps(body), PASSWORD))


def plain_reply(body, status):
    return Mock(status_code=status, text=json.dumps(body))


@unittest.skipUnless(DB["database"], "set TEST_DB_NAME (and TEST_DB_*) to run database tests")
class SessionFlowTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connection = cls.connect()
        cursor = connection.cursor()
        for table in ("user_download_history", "app_sessions", "app_users"):
            cursor.execute(f"DROP TABLE IF EXISTS {table}")
        connection.commit()
        connection.close()
        with patch("app.repositories.account_repository.get_connection", side_effect=cls.connect):
            account_repository.create_tables()
            account_repository.create_tables()  # safe to run twice

    @staticmethod
    def connect():
        return mysql.connector.connect(**DB)

    def setUp(self):
        connection = self.connect()
        cursor = connection.cursor()
        for table in ("user_download_history", "app_sessions", "app_users"):
            cursor.execute(f"DELETE FROM {table}")
        connection.commit()
        connection.close()

        for p in (
            patch("app.repositories.account_repository.get_connection", side_effect=self.connect),
            patch.object(settings, "auth_service_base_url", "https://auth.example.com"),
            patch.object(settings, "auth_service_app", "insta-save"),
            patch.object(settings, "auth_service_rncryptor_key", KEY),
            patch.object(settings, "auth_service_rncryptor_offset", 8),
            patch.object(settings, "auth_service_rncryptor_length", 32),
            patch.object(settings, "client_encryption_key", CLIENT_KEY),
            patch.object(settings, "encryption_key", "test-app-encryption-key"),
        ):
            p.start()
            self.addCleanup(p.stop)

        # Fake auth service: who logs in, and what GET /api/v2/user answers.
        self.login_user = auth_user()
        self.user_check = lambda: encrypted_reply({"code": 200, "message": "OK", "data": {"user": self.login_user}})
        request_patch = patch("app.services.auth_service.requests.request", side_effect=self._auth_service)
        self.auth_requests = request_patch.start()
        self.addCleanup(request_patch.stop)

        self.client = TestClient(create_app())

    def _auth_service(self, method, url, **kwargs):
        if url.endswith("/api/v2/user"):
            return self.user_check()
        return encrypted_reply({"message": "Logged In successfully.", "code": 200, "data": {"user": self.login_user}})

    # ----- helpers -----

    def auth_call(self, path, bearer=None, **fields):
        headers = {"Content-Type": "text/plain"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        body = client_crypto.encrypt(json.dumps(fields)) if fields else ""
        response = self.client.post(f"/auth/{path}", content=body, headers=headers)
        return response.status_code, json.loads(client_crypto.decrypt(response.text))

    def guest(self, device="device-A"):
        status, body = self.auth_call("guest-login", deviceId=device, deviceType="web")
        self.assertEqual(status, 200, body)
        return body["data"]

    def google(self, bearer=None, **fields):
        status, body = self.auth_call("google", bearer, token="google-id-token", **fields)
        self.assertEqual(status, 200, body)
        return body["data"]

    def save(self, token, url, **item):
        response = self.client.post(
            "/history", json={"url": url, "item": {"originalUrl": url, **item}},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def history(self, token):
        response = self.client.get("/history", headers={"Authorization": f"Bearer {token}"})
        return response.status_code, response.json()

    def urls(self, token):
        status, body = self.history(token)
        self.assertEqual(status, 200, body)
        return [entry["url"] for entry in body["data"]["items"]]


class GuestToAccountFlowTests(SessionFlowTestCase):
    def test_full_flow_guest_login_logout_new_guest(self):
        # 1. No login: guest history.
        guest = self.guest()
        self.assertTrue(guest["is_guest"])
        self.assertIsNone(guest["user"])
        self.save(guest["token"], "https://instagram.com/reel/one")

        # 2. Login with the guest token: history moves to the account.
        account = self.google(guest["token"])
        self.assertFalse(account["is_guest"])
        self.assertNotEqual(account["user_id"], guest["user_id"])
        self.assertEqual(account["user"]["email"], "user6@example.com")
        self.assertEqual(self.urls(account["token"]), ["https://instagram.com/reel/one"])
        self.assertEqual(self.history(guest["token"])[0], 401)  # old guest token is dead

        # 3. Logout: the account token stops working.
        status, body = self.auth_call("logout", account["token"])
        self.assertEqual((status, body["code"]), (200, 200))
        self.assertEqual(self.history(account["token"])[0], 401)

        # 4. Guest login again on the same device: a new guest that does NOT see the account's history.
        new_guest = self.guest()
        self.assertNotEqual(new_guest["user_id"], guest["user_id"])
        self.assertEqual(self.urls(new_guest["token"]), [])

        # 5. Logging in again shows the account's history plus the new guest's.
        self.save(new_guest["token"], "https://instagram.com/reel/two")
        again = self.google(new_guest["token"])
        self.assertEqual(again["user_id"], account["user_id"])
        self.assertEqual(
            self.urls(again["token"]), ["https://instagram.com/reel/two", "https://instagram.com/reel/one"]
        )

    def test_guest_login_is_stable_per_device_and_separate_between_devices(self):
        a1 = self.guest("device-A")
        a2 = self.guest("device-A")
        b = self.guest("device-B")
        self.assertEqual(a1["user_id"], a2["user_id"])
        self.assertNotEqual(a1["user_id"], b["user_id"])

        self.save(a1["token"], "https://instagram.com/p/a")
        self.assertEqual(self.urls(a2["token"]), ["https://instagram.com/p/a"])
        self.assertEqual(self.urls(b["token"]), [])

    def test_guest_login_reuses_a_valid_guest_token(self):
        first = self.guest()
        status, body = self.auth_call("guest-login", first["token"], deviceId="device-A")
        self.assertEqual(body["data"]["token"], first["token"])

    def test_same_url_on_guest_and_account_is_merged_once_with_newest_item(self):
        account = self.google()
        self.save(account["token"], "https://instagram.com/p/x", caption="old")
        self.auth_call("logout", account["token"])

        guest = self.guest()
        self.save(guest["token"], "https://instagram.com/p/x", caption="new")
        again = self.google(guest["token"])

        status, body = self.history(again["token"])
        items = body["data"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["item"]["caption"], "new")

    def test_login_without_guest_token_leaves_device_guest_alone(self):
        guest = self.guest()
        self.save(guest["token"], "https://instagram.com/p/g")
        account = self.google()
        self.assertEqual(self.urls(account["token"]), [])
        self.assertEqual(self.urls(guest["token"]), ["https://instagram.com/p/g"])

    def test_account_on_two_devices_shares_history_and_logs_out_separately(self):
        phone = self.google(self.guest("phone")["token"])
        web = self.google(self.guest("web")["token"])
        self.save(phone["token"], "https://instagram.com/p/from-phone")
        self.assertEqual(self.urls(web["token"]), ["https://instagram.com/p/from-phone"])

        self.auth_call("logout", phone["token"])
        self.assertEqual(self.history(phone["token"])[0], 401)
        self.assertEqual(self.history(web["token"])[0], 200)

    def test_switching_accounts_keeps_histories_apart(self):
        first = self.google(self.guest()["token"])
        self.save(first["token"], "https://instagram.com/p/first")
        self.login_user = auth_user(7, "auth-key-7", "Other")
        second = self.google(first["token"])
        self.assertNotEqual(second["user_id"], first["user_id"])
        self.assertEqual(self.urls(second["token"]), [])
        self.assertEqual(self.history(first["token"])[0], 401)

    def test_rejected_login_keeps_the_guest(self):
        guest = self.guest()
        self.save(guest["token"], "https://instagram.com/p/g")
        self.auth_requests.side_effect = lambda *a, **k: plain_reply({"code": 401, "message": "Invalid token."}, 401)

        status, body = self.auth_call("google", guest["token"], token="bad")

        self.assertEqual((status, body["message"]), (401, "Invalid token."))
        self.assertEqual(self.urls(guest["token"]), ["https://instagram.com/p/g"])

    def test_auth_key_is_stored_encrypted(self):
        self.google()
        connection = self.connect()
        cursor = connection.cursor()
        cursor.execute("SELECT auth_key FROM app_users WHERE is_guest = 0")
        stored = cursor.fetchone()[0]
        connection.close()
        self.assertTrue(stored.startswith("enc:"))
        self.assertNotIn("auth-key-6", stored)


class AutoLoginTests(SessionFlowTestCase):
    def test_account_token_returns_fresh_user(self):
        account = self.google()
        self.login_user = auth_user(name="Renamed")

        status, body = self.auth_call("auto-login", account["token"])

        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["token"], account["token"])
        self.assertEqual(body["data"]["user"]["name"], "Renamed")
        url, kwargs = self.auth_requests.call_args[0][1], self.auth_requests.call_args[1]
        self.assertTrue(url.endswith("/api/v2/user"))
        self.assertEqual(rncryptor.decrypt(kwargs["headers"]["Auth-Key"], PASSWORD), "auth-key-6")

    def test_auth_key_rejected_by_auth_service_logs_out(self):
        account = self.google()
        self.user_check = lambda: plain_reply({"code": 401, "message": "Unauthorized"}, 401)

        status, body = self.auth_call("auto-login", account["token"])

        self.assertEqual(status, 401)
        self.assertEqual(self.history(account["token"])[0], 401)

    def test_auth_service_down_keeps_user_logged_in(self):
        account = self.google()

        def down():
            raise requests.ConnectionError()

        self.user_check = down
        status, body = self.auth_call("auto-login", account["token"])
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["user"]["email"], "user6@example.com")

    def test_guest_token_returns_guest(self):
        guest = self.guest()
        status, body = self.auth_call("auto-login", guest["token"])
        self.assertEqual((status, body["data"]["is_guest"]), (200, True))
        self.auth_requests.assert_not_called()

    def test_missing_unknown_or_logged_out_token_is_401(self):
        account = self.google()
        self.auth_call("logout", account["token"])
        for token in (None, "unknown-token", account["token"]):
            with self.subTest(token=token):
                status, body = self.auth_call("auto-login", token)
                self.assertEqual((status, body["code"]), (401, 401))


class HistoryTests(SessionFlowTestCase):
    def test_requires_a_token(self):
        for call in (
            lambda: self.client.get("/history"),
            lambda: self.client.post("/history", json={"url": "u", "item": {}}),
            lambda: self.client.delete("/history", headers={"Authorization": "Bearer nope"}),
        ):
            response = call()
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {"code": 401, "data": None, "message": "Unauthorized"})

    def test_saving_again_moves_to_top_and_delete_and_clear_work(self):
        token = self.guest()["token"]
        for url in ("u1", "u2", "u3"):
            self.save(token, url)
        self.save(token, "u1", caption="again")
        self.assertEqual(self.urls(token), ["u1", "u3", "u2"])

        response = self.client.post("/history/delete", json={"url": "u2"}, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.json()["data"], {"deleted": True})
        self.assertEqual(self.urls(token), ["u1", "u3"])

        response = self.client.delete("/history", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.json()["data"], {"deleted": 2})
        self.assertEqual(self.urls(token), [])

    def test_only_newest_items_are_kept(self):
        token = self.guest()["token"]
        with patch.object(account_repository, "HISTORY_LIMIT", 3):
            for url in ("u1", "u2", "u3", "u4", "u5"):
                self.save(token, url)
        self.assertEqual(self.urls(token), ["u5", "u4", "u3"])

    def test_bad_items_are_rejected(self):
        token = self.guest()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        for payload in ({"url": "", "item": {}}, {"url": "u"}, {"url": "u", "item": "not an object"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/history", json=payload, headers=headers).status_code, 422)
        big = {"url": "u", "item": {"x": "a" * 70000}}
        response = self.client.post("/history", json=big, headers=headers)
        self.assertEqual((response.status_code, response.json()["message"]), (422, "History item is too large."))

    def import_items(self, token, items):
        return self.client.post("/history/import", json={"items": items}, headers={"Authorization": f"Bearer {token}"})

    def test_import_keeps_order_and_a_retry_adds_nothing(self):
        token = self.guest()["token"]
        items = [{"url": f"app-file:{n}", "item": {"source": "app", "fileName": n}} for n in ("old", "mid", "new")]

        first = self.import_items(token, items)
        again = self.import_items(token, items)

        self.assertEqual(first.json()["data"], {"received": 3, "added": 3})
        self.assertEqual(again.json()["data"], {"received": 3, "added": 0})
        self.assertEqual(self.urls(token), ["app-file:new", "app-file:mid", "app-file:old"])

    def test_imported_history_moves_to_the_account_on_login(self):
        guest = self.guest()
        self.import_items(guest["token"], [{"url": "app-file:a", "item": {"source": "app"}}])
        account = self.google(guest["token"])
        self.assertEqual(self.urls(account["token"]), ["app-file:a"])

    def test_import_is_limited(self):
        token = self.guest()["token"]
        too_many = [{"url": f"u{i}", "item": {}} for i in range(account_repository.HISTORY_LIMIT + 1)]
        self.assertEqual(self.import_items(token, too_many).status_code, 422)
        big = [{"url": "u", "item": {"x": "a" * 70000}}]
        self.assertEqual(self.import_items(token, big).status_code, 422)
        self.assertEqual(self.import_items(token, []).json()["data"], {"received": 0, "added": 0})

    def test_web_clear_keeps_app_entries(self):
        token = self.guest()["token"]
        self.save(token, "https://instagram.com/p/web")
        self.import_items(token, [{"url": "app-file:phone", "item": {"source": "app"}}])

        response = self.client.delete("/history?scope=web", headers={"Authorization": f"Bearer {token}"})

        self.assertEqual(response.json()["data"], {"deleted": 1})
        self.assertEqual(self.urls(token), ["app-file:phone"])
        self.client.delete("/history", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(self.urls(token), [])
        bad = self.client.delete("/history?scope=nope", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(bad.status_code, 422)

    def test_guest_login_needs_device_id(self):
        status, body = self.auth_call("guest-login", deviceType="web")
        self.assertEqual((status, body["code"]), (422, 422))


if __name__ == "__main__":
    unittest.main()
