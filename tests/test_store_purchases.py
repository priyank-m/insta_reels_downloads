"""App store purchases bound to guests / accounts, against a real MySQL/MariaDB (stores faked).

Needs an empty throwaway database: TEST_DB_HOST/PORT/USER/PASSWORD/NAME (tables are dropped).
"""
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import mysql.connector
from fastapi.testclient import TestClient

from encrypted_client import EncryptedClient
from app.core.config import settings
from app.main import create_app
from app.repositories import account_repository, payment_repository
from app.services import payment_service, store_service
from app.services import store_verification as stores


DB = {
    "host": os.getenv("TEST_DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("TEST_DB_PORT", "3306")),
    "user": os.getenv("TEST_DB_USER", "root"),
    "password": os.getenv("TEST_DB_PASSWORD", ""),
    "database": os.getenv("TEST_DB_NAME", ""),
}
TABLES = ("store_purchases", "payment_webhooks", "subscriptions", "payment_links", "subscription_plans",
          "user_download_history", "app_sessions", "app_users")
PLUS_ID = "com.instasaver.saveandrepost.month2"
PREMIUM_ID = "com.instasaver.saveandrepost.year"


def now():
    return datetime.utcnow().replace(microsecond=0)


def entitlement(key="1000001", product=PLUS_ID, days=30, renew=True):
    return stores.StoreEntitlement(store_key=key, product_id=product, expires=now() + timedelta(days=days),
                                   is_auto_renew=renew, is_cancelled=False, environment="sandbox")


@unittest.skipUnless(DB["database"], "set TEST_DB_NAME (and TEST_DB_*) to run database tests")
class StorePurchaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connection = mysql.connector.connect(**DB)
        cursor = connection.cursor()
        for table in TABLES:
            cursor.execute(f"DROP TABLE IF EXISTS {table}")
        connection.commit()
        connection.close()
        with patch("app.repositories.account_repository.get_connection", side_effect=lambda: mysql.connector.connect(**DB)):
            account_repository.create_tables()
            payment_repository.create_tables()

    def setUp(self):
        connection = mysql.connector.connect(**DB)
        cursor = connection.cursor()
        for table in TABLES:
            if table != "subscription_plans":
                cursor.execute(f"DELETE FROM {table}")
        connection.commit()
        connection.close()
        for p in (
            patch("app.repositories.account_repository.get_connection", side_effect=lambda: mysql.connector.connect(**DB)),
            patch.object(settings, "client_encryption_key", "client-key-shared-with-the-apps"),
            patch.object(settings, "ai_requires_subscription", True),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.apple = [entitlement()]
        self.google = [entitlement(key="google-token-1", product="com.video.downloader.save.manage.pp.mo")]
        for name, attr in (("verify_apple", "apple"), ("verify_google", "google")):
            p = patch(f"app.services.store_verification.{name}", side_effect=lambda *a, _attr=attr: self._answer(_attr))
            self.addCleanup(p.stop)
            setattr(self, f"{attr}_mock", p.start())
        self.client = EncryptedClient(TestClient(create_app()))

    def _answer(self, attr):
        value = getattr(self, attr)
        if isinstance(value, Exception):
            raise value
        return value

    # ----- helpers -----

    def guest(self, device="phone-1"):
        with account_repository.transaction() as cursor:
            uid = account_repository.get_or_create_guest(cursor, device, "ios")
            return uid, account_repository.create_session(cursor, uid, device, "ios")

    def account(self, auth_id="6"):
        with account_repository.transaction() as cursor:
            uid = account_repository.upsert_account(cursor, auth_id, "google", "enc:x", "User", f"u{auth_id}@x.com", {})
            return uid, account_repository.create_session(cursor, uid, "phone-1", "ios")

    def login(self, guest_id, auth_id="6"):
        account_id, token = self.account(auth_id)
        with account_repository.transaction() as cursor:
            account_repository.merge_guest_into_account(cursor, guest_id, account_id)
        return account_id, token

    def verify(self, token, **body):
        body = body or {"platform": "apple", "receipt": "base64-receipt"}
        return self.client.post("/store/verify", json=body, headers={"Authorization": f"Bearer {token}"})

    def ents(self, token):
        return self.client.get("/store/entitlements", headers={"Authorization": f"Bearer {token}"}).json()["data"]

    # ----- binding -----

    def test_guest_purchase_moves_to_the_account_on_login(self):
        guest_id, guest_token = self.guest()
        data = self.verify(guest_token).json()["data"]
        self.assertEqual((data["plus"], data["premium"], data["owned_by_other_account"]), (True, False, False))

        _, account_token = self.login(guest_id)
        self.assertTrue(self.ents(account_token)["plus"])

    def test_after_logout_the_new_guest_does_not_get_the_account_purchase(self):
        guest_id, guest_token = self.guest()
        self.verify(guest_token)
        self.login(guest_id)

        _, new_guest_token = self.guest()  # same phone, after logout
        response = self.verify(new_guest_token)

        data = response.json()["data"]
        self.assertEqual((data["plus"], data["owned_by_other_account"]), (False, True))
        self.assertIn("another account", response.json()["message"])
        self.assertFalse(self.ents(new_guest_token)["plus"])

    def test_another_account_on_the_same_phone_does_not_get_it(self):
        guest_id, guest_token = self.guest()
        self.verify(guest_token)
        self.login(guest_id, auth_id="6")
        _, other_token = self.account(auth_id="7")
        self.assertTrue(self.verify(other_token).json()["data"]["owned_by_other_account"])

    def test_a_guest_owned_purchase_can_be_taken_over(self):
        _, first_phone = self.guest("phone-1")
        self.verify(first_phone)
        _, second_phone = self.guest("phone-2")  # same store account, not logged in
        self.assertTrue(self.verify(second_phone).json()["data"]["plus"])
        self.assertFalse(self.ents(first_phone)["plus"])

    def test_google_purchase_and_tiers(self):
        _, token = self.guest()
        self.google = [entitlement(key="t", product="com.video.downloader.saver.manager.week")]
        data = self.verify(token, platform="google", purchase_token="t").json()["data"]
        self.assertEqual((data["premium"], data["plus"]), (True, False))
        self.google_mock.assert_called_once_with("t")

    def test_unknown_product_gives_nothing(self):
        _, token = self.guest()
        self.apple = [entitlement(product="com.someone.else")]
        data = self.verify(token).json()["data"]
        self.assertEqual((data["premium"], data["plus"]), (False, False))

    def test_store_errors(self):
        _, token = self.guest()
        self.apple = stores.InvalidPurchase("bad")
        self.assertEqual(self.verify(token).status_code, 422)
        self.apple = stores.StoreUnavailable("down")
        self.assertEqual(self.verify(token).status_code, 503)
        self.assertEqual(self.verify(token, platform="apple").status_code, 422)  # no receipt
        self.assertEqual(self.verify(token, platform="windows", receipt="x").status_code, 422)
        self.assertEqual(self.client.post("/store/verify", json={"platform": "apple", "receipt": "x"}).status_code, 401)

    def test_lapsed_purchase_is_rechecked_with_the_store(self):
        _, token = self.guest()
        self.apple = [entitlement(days=-1)]  # expired at last check
        self.assertFalse(self.verify(token).json()["data"]["plus"])
        with account_repository.transaction() as cursor:
            cursor.execute("UPDATE store_purchases SET checked_at = %s", (now() - timedelta(hours=7),))
        self.apple = [entitlement(days=30)]  # renewed in the meantime
        self.assertTrue(self.ents(token)["plus"])
        self.assertEqual(self.apple_mock.call_args[0][0], "base64-receipt")  # the stored receipt

    # ----- web <-> app -----

    def test_web_plan_gives_plus_in_the_app(self):
        account_id, token = self.account()
        with account_repository.transaction() as cursor:
            plan = payment_repository.get_plan(cursor, "ai-monthly")
            payment_repository.insert_subscription(cursor, {
                "user_id": account_id, "origin_transaction_id": "tok", "subscription_plan_id": plan["id"],
                "expires_date": now() + timedelta(days=30), "is_auto_renew": 1,
            })
        data = self.ents(token)
        self.assertTrue(data["plus"])
        self.assertEqual(data["sources"][0]["platform"], "web")

    def test_app_plus_opens_web_ai_tools_and_blocks_a_second_plan(self):
        guest_id, guest_token = self.guest()
        self.verify(guest_token)
        account_id, token = self.login(guest_id)

        self.assertTrue(payment_service.has_active_subscription(account_id))
        sub = self.client.get("/payment/my-subscription", headers={"Authorization": f"Bearer {token}"}).json()["data"]
        self.assertEqual((sub["platform"], sub["plan_name"]), ("apple", "Creator Plus (App Store)"))
        with patch.object(settings, "payment_url", "https://pay"), patch.object(settings, "payment_project_code", "1"), \
                patch.object(settings, "razorpay_key_id", "k"):
            with account_repository.transaction() as cursor:
                cursor.execute("UPDATE subscription_plans SET price_id = 'plan_x' WHERE slug = 'ai-monthly'")
            response = self.client.post("/payment/process/ai-monthly", json={}, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.json()["data"], {"has_active_subscription": True})

    def test_premium_only_does_not_open_ai_tools(self):
        _, token = self.guest()
        self.apple = [entitlement(product=PREMIUM_ID)]
        self.verify(token)
        with account_repository.transaction() as cursor:
            uid = account_repository.find_session(cursor, token)["id"]
        self.assertFalse(store_service.has_store_plus(uid))


if __name__ == "__main__":
    unittest.main()
