"""AI plan payments against a real MySQL/MariaDB, with the payment microservice faked.

Needs an empty throwaway database (payment and account tables are dropped and re-created):
    TEST_DB_HOST=127.0.0.1 TEST_DB_PORT=3306 TEST_DB_USER=... TEST_DB_PASSWORD=... TEST_DB_NAME=isave_ci
"""
import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import mysql.connector
from fastapi.testclient import TestClient

from encrypted_client import EncryptedClient

from app.core.config import settings
from app.main import create_app
from app.repositories import account_repository, payment_repository
from app.services import payment_service


DB = {
    "host": os.getenv("TEST_DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("TEST_DB_PORT", "3306")),
    "user": os.getenv("TEST_DB_USER", "root"),
    "password": os.getenv("TEST_DB_PASSWORD", ""),
    "database": os.getenv("TEST_DB_NAME", ""),
}
TABLES = ("payment_webhooks", "subscriptions", "payment_links", "subscription_plans",
          "user_download_history", "app_sessions", "app_users")
WEBHOOK_KEY = "webhook-key-for-tests"


def fmt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


class DateParsingTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(payment_service._date("2026-08-11 10:15:00"), datetime(2026, 8, 11, 10, 15))
        self.assertEqual(payment_service._date("2026-08-11T10:15:00Z"), datetime(2026, 8, 11, 10, 15))
        self.assertEqual(payment_service._date("2026-08-11T15:45:00+05:30"), datetime(2026, 8, 11, 10, 15))
        self.assertEqual(payment_service._date(0), datetime(1970, 1, 1))
        for bad in (None, "", "soon", "2026-13-40"):
            self.assertIsNone(payment_service._date(bad))

    def test_test_user_matching(self):
        with patch.object(settings, "test_payment_user_emails", " QA@x.com, ,b@y.com,"):
            self.assertTrue(payment_service.is_test_payment_user("qa@X.com "))
            self.assertFalse(payment_service.is_test_payment_user(""))
            self.assertFalse(payment_service.is_test_payment_user(None))
            self.assertFalse(payment_service.is_test_payment_user("c@z.com"))


@unittest.skipUnless(DB["database"], "set TEST_DB_NAME (and TEST_DB_*) to run database tests")
class PaymentFlowTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connection = cls.connect()
        cursor = connection.cursor()
        for table in TABLES:
            cursor.execute(f"DROP TABLE IF EXISTS {table}")
        connection.commit()
        connection.close()
        with patch("app.repositories.account_repository.get_connection", side_effect=cls.connect):
            account_repository.create_tables()
            payment_repository.create_tables()

    @staticmethod
    def connect():
        return mysql.connector.connect(**DB)

    def setUp(self):
        connection = self.connect()
        cursor = connection.cursor()
        for table in TABLES:
            if table != "subscription_plans":
                cursor.execute(f"DELETE FROM {table}")
        cursor.execute("UPDATE subscription_plans SET price_id = NULL, is_active = 1 WHERE slug <> 'recurring-test'")
        cursor.execute("UPDATE subscription_plans SET price_id = 'plan_ai_monthly' WHERE slug = 'ai-monthly'")
        cursor.execute("UPDATE subscription_plans SET price_id = 'plan_test_1rs' WHERE slug = 'recurring-test'")
        connection.commit()
        connection.close()

        for p in (
            patch("app.repositories.account_repository.get_connection", side_effect=self.connect),
            patch.object(settings, "payment_url", "https://pay.example.com/api"),
            patch.object(settings, "payment_project_code", "003099"),
            patch.object(settings, "payment_project_slug", "insta_save"),
            patch.object(settings, "razorpay_key_id", "rzp_test_key"),
            patch.object(settings, "client_encryption_key", "client-key-shared-with-the-apps"),
            patch.object(settings, "payment_webhook_key", WEBHOOK_KEY),
            patch.object(settings, "test_payment_user_emails", "qa@example.com"),
            patch.object(settings, "ai_requires_subscription", False),
        ):
            p.start()
            self.addCleanup(p.stop)

        self.gateway_answer = {"status": True, "data": {
            "token": "tok_1", "url": "https://rzp.io/i/abc?sub=sub_FROMURL1", "subscription_id": "sub_ABC"}}
        create = patch("app.services.payment_gateway.create_payment", side_effect=lambda data: self.gateway_answer)
        self.create_payment = create.start()
        self.addCleanup(create.stop)
        cancel = patch("app.services.payment_gateway.cancel_subscription", return_value={"status": 1})
        self.cancel = cancel.start()
        self.addCleanup(cancel.stop)

        # New APIs are encrypted both ways, like the apps call them
        self.client = EncryptedClient(TestClient(create_app()))

    # ----- helpers -----

    def account(self, email="user@example.com", auth_user_id="6"):
        with account_repository.transaction() as cursor:
            user_id = account_repository.upsert_account(cursor, auth_user_id, "google", "enc:x", "User", email, {})
            token = account_repository.create_session(cursor, user_id, "dev", "web")
        return user_id, token

    def guest(self):
        with account_repository.transaction() as cursor:
            guest_id = account_repository.get_or_create_guest(cursor, "dev-guest", "web")
            return account_repository.create_session(cursor, guest_id, "dev-guest", "web")

    def buy(self, token, slug="ai-monthly", **body):
        return self.client.post(f"/payment/process/{slug}", json=body or None, headers={"Authorization": f"Bearer {token}"})

    def webhook(self, key=WEBHOOK_KEY, wrap=True, **data):
        body = {
            "customer_payment_id": "tok_1",
            "subscription_type": "RECURRING",
            "payment_status": "SUCCEEDED",
            "subscription_status": "ACTIVE",
            "subscription_id": "sub_ABC",
            "subscription_start_date": fmt(self.start),
            "subscription_end_date": fmt(self.end),
            "trial_period": 0,
            "total_amount": 149,
            "currency": "INR",
            "payment_type": "upi",
            **data,
        }
        return self.client.post(f"/payment/webhook/{key}", json={"data": body} if wrap else body)

    start = datetime.utcnow().replace(microsecond=0) - timedelta(minutes=1)
    end = start + timedelta(days=30)

    def rows(self, sql, params=()):
        connection = self.connect()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        connection.close()
        return rows

    def subscription(self, token):
        return self.client.get("/payment/my-subscription", headers={"Authorization": f"Bearer {token}"}).json()["data"]


class PlanAndCheckoutTests(PaymentFlowTestCase):
    def test_plans_list_hides_the_test_plan(self):
        plans = self.client.get("/payment/plans").json()["data"]
        self.assertEqual([p["slug"] for p in plans], ["ai-weekly", "ai-monthly", "ai-yearly"])
        monthly = plans[1]
        self.assertEqual((monthly["price"], monthly["interval"], monthly["is_popular"], monthly["is_available"]),
                         (149, "month", True, True))
        self.assertFalse(plans[0]["is_available"])  # no Razorpay plan id yet

    def test_checkout_is_returned_and_payment_link_stored(self):
        user_id, token = self.account()

        response = self.buy(token)

        self.assertEqual(response.status_code, 200, response.text)
        checkout = response.json()["data"]["checkout"]
        self.assertEqual(checkout["token"], "tok_1")
        self.assertEqual(checkout["key"], "rzp_test_key")
        self.assertEqual(checkout["subscription_id"], "sub_ABC")
        self.assertEqual(checkout["prefill"]["email"], "user@example.com")
        sent = self.create_payment.call_args[0][0]
        self.assertEqual((sent["plan_id"], sent["interval"], sent["subscription_type"], sent["project_code"]),
                         ("plan_ai_monthly", "month", "RECURRING", "003099"))
        link = self.rows("SELECT * FROM payment_links")[0]
        self.assertEqual((link["user_id"], link["payment_id"]), (user_id, "tok_1"))

    def test_subscription_id_is_read_from_the_url_when_missing(self):
        _, token = self.account()
        del self.gateway_answer["data"]["subscription_id"]
        self.assertEqual(self.buy(token).json()["data"]["checkout"]["subscription_id"], "sub_FROMURL1")

    def test_guest_and_anonymous_cannot_buy(self):
        self.assertEqual(self.buy(self.guest()).status_code, 401)
        self.assertEqual(self.client.post("/payment/process/ai-monthly").status_code, 401)
        self.create_payment.assert_not_called()

    def test_unknown_unavailable_and_test_plans_are_rejected(self):
        _, token = self.account()
        for slug, message in (("nope", "Invalid plan selected."), ("recurring-test", "Invalid plan selected."),
                              ("ai-weekly", "This plan is not available for purchase yet. Please try again later.")):
            with self.subTest(slug):
                response = self.buy(token, slug)
                self.assertEqual((response.status_code, response.json()["message"]), (400, message))
        self.create_payment.assert_not_called()

    def test_email_is_required_and_saved_when_the_account_has_none(self):
        user_id, token = self.account(email=None)
        response = self.buy(token)
        self.assertEqual(response.json()["data"], {"requires_email": True})
        self.assertEqual(self.buy(token, email="not-an-email").json()["data"], {"requires_email": True})

        self.assertEqual(self.buy(token, email="new@example.com").status_code, 200)
        self.assertEqual(self.rows("SELECT email FROM app_users WHERE id = %s", (user_id,))[0]["email"], "new@example.com")

    def test_test_user_is_charged_the_test_plan_but_keeps_the_chosen_plan(self):
        _, token = self.account(email="QA@example.com")
        self.buy(token)
        self.assertEqual(self.create_payment.call_args[0][0]["plan_id"], "plan_test_1rs")
        link = self.rows("SELECT p.slug FROM payment_links l JOIN subscription_plans p ON p.id = l.subscription_plan_id")
        self.assertEqual(link[0]["slug"], "ai-monthly")

    def test_posting_a_test_email_does_not_give_test_pricing(self):
        _, token = self.account(email=None)
        self.buy(token, email="qa@example.com")
        self.assertEqual(self.create_payment.call_args[0][0]["plan_id"], "plan_ai_monthly")

    def test_active_plan_blocks_a_second_purchase(self):
        _, token = self.account()
        self.buy(token)
        self.webhook()
        response = self.buy(token)
        self.assertEqual(response.json()["data"], {"has_active_subscription": True})

    def test_gateway_failures_are_400(self):
        _, token = self.account()
        for answer in ({"status": False}, {"status": True, "data": {"token": "t"}}):
            with self.subTest(answer=answer):
                self.gateway_answer = answer
                self.assertEqual(self.buy(token).status_code, 400)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM payment_links")[0]["n"], 0)

    def test_unconfigured_payments_are_503(self):
        _, token = self.account()
        with patch.object(settings, "payment_url", ""):
            self.assertEqual(self.buy(token).status_code, 503)


class WebhookTests(PaymentFlowTestCase):
    def setUp(self):
        super().setUp()
        self.user_id, self.token = self.account()
        self.buy(self.token)

    def test_first_purchase_creates_an_active_subscription(self):
        self.assertEqual(self.webhook().status_code, 200)

        sub = self.subscription(self.token)
        self.assertEqual((sub["plan_slug"], sub["is_active"], sub["is_auto_renew"]), ("ai-monthly", True, False))
        row = self.rows("SELECT * FROM subscriptions")[0]
        self.assertTrue(row["transaction_id"].startswith("TXT_"))
        self.assertEqual((row["razorpay_subscription_id"], row["razorpay_payment_type"], row["price"]),
                         ("sub_ABC", "upi", 149))
        status = self.client.get("/payment/status/tok_1").json()["data"]
        self.assertTrue(status["is_active"])

    def test_raw_event_is_logged_with_the_user(self):
        self.webhook()
        log = self.rows("SELECT * FROM payment_webhooks")[0]
        self.assertEqual((log["user_id"], log["customer_payment_id"]), (self.user_id, "tok_1"))
        self.assertEqual(json.loads(log["payload"])["data"]["payment_type"], "upi")

    def test_flat_body_without_data_wrapper_works(self):
        self.webhook(wrap=False)
        self.assertTrue(self.subscription(self.token)["is_active"])

    def test_duplicate_webhook_creates_one_row(self):
        self.webhook()
        self.webhook()
        self.assertEqual(len(self.rows("SELECT id FROM subscriptions")), 1)

    def test_renewal_creates_a_new_row_carrying_the_origin(self):
        self.webhook()
        later = self.end + timedelta(days=30)
        self.webhook(subscription_start_date=fmt(self.end), subscription_end_date=fmt(later))
        self.webhook(subscription_start_date=fmt(self.end), subscription_end_date=fmt(later))  # repeat

        first, renewal = self.rows("SELECT * FROM subscriptions ORDER BY id")
        self.assertNotEqual(first["transaction_id"], renewal["transaction_id"])
        self.assertEqual(renewal["origin_transaction_id"], "tok_1")
        self.assertEqual(renewal["original_purchase_date"], first["original_purchase_date"])
        self.assertEqual(renewal["purchase_date"], first["expires_date"])
        self.assertEqual((renewal["expires_date"], renewal["is_auto_renew"]), (later, 1))

    def test_cancellation_updates_without_ending_access(self):
        self.webhook()
        self.webhook(payment_status="CANCELLED", subscription_status="CANCELLED", cancel_sub_at=fmt(self.start))
        rows = self.rows("SELECT * FROM subscriptions")
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["cancellation_date"])
        self.assertTrue(self.subscription(self.token)["is_active"])

    def test_cancel_signalled_only_by_subscription_status_is_applied(self):
        self.webhook()
        self.webhook(payment_status="", subscription_status="HALTED", cancel_sub_request_date=fmt(self.start))
        self.assertIsNotNone(self.rows("SELECT cancellation_date FROM subscriptions")[0]["cancellation_date"])

    def test_late_null_fields_do_not_wipe_earlier_values(self):
        self.webhook()
        self.webhook(subscription_id=None, payment_type=None, total_amount=None)
        row = self.rows("SELECT * FROM subscriptions")[0]
        self.assertEqual((row["razorpay_subscription_id"], row["razorpay_payment_type"]), ("sub_ABC", "upi"))

    def test_late_older_webhook_does_not_shorten_the_period(self):
        self.webhook()
        self.webhook(subscription_end_date=fmt(self.end - timedelta(days=10)))
        self.assertEqual(self.rows("SELECT expires_date FROM subscriptions")[0]["expires_date"], self.end)

    def test_ignored_events(self):
        cases = {
            "initiated": dict(payment_status="INITIATED", subscription_status="CREATED"),
            "unknown payment": dict(customer_payment_id="tok_unknown"),
            "no id": dict(customer_payment_id=None),
            "one-time": dict(subscription_type="ONE_TIME"),
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assertEqual(self.webhook(**data).status_code, 200)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM subscriptions")[0]["n"], 0)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM payment_webhooks")[0]["n"], 4)

    def test_wrong_key_is_rejected_and_not_stored(self):
        self.assertEqual(self.webhook(key="wrong").status_code, 404)
        with patch.object(settings, "payment_webhook_key", ""):
            self.assertEqual(self.webhook().status_code, 404)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM payment_webhooks")[0]["n"], 0)

    def test_db_down_returns_500_so_it_is_retried(self):
        with patch("app.repositories.account_repository.get_connection", return_value=None):
            self.assertEqual(self.webhook().status_code, 500)

    def test_expired_plan_is_not_active(self):
        past_end = datetime.utcnow().replace(microsecond=0) - timedelta(days=1)
        self.webhook(subscription_start_date=fmt(past_end - timedelta(days=30)), subscription_end_date=fmt(past_end))
        self.assertIsNone(self.subscription(self.token))
        self.assertFalse(self.client.get("/payment/status/tok_1").json()["data"]["is_active"])
        self.assertEqual(self.buy(self.token).status_code, 200)  # can buy again


class CancelAndAccessTests(PaymentFlowTestCase):
    def setUp(self):
        super().setUp()
        self.user_id, self.token = self.account()
        self.buy(self.token)
        self.webhook()

    def cancel_call(self):
        return self.client.post("/payment/cancel-subscription", headers={"Authorization": f"Bearer {self.token}"})

    def test_cancel_stops_renewal_and_keeps_access(self):
        response = self.cancel_call()
        self.assertEqual(response.status_code, 200, response.text)
        self.cancel.assert_called_once_with("sub_ABC")
        sub = self.subscription(self.token)
        self.assertEqual((sub["is_active"], sub["is_auto_renew"]), (True, False))
        self.assertIsNotNone(sub["cancellation_date"])
        self.assertEqual(self.cancel_call().json()["message"], "Your plan is already cancelled.")

    def test_gateway_refusal_keeps_the_plan_renewing(self):
        self.cancel.return_value = {"status": 0, "message": "Nope"}
        self.assertEqual(self.cancel_call().json()["message"], "Nope")
        self.assertIsNone(self.subscription(self.token)["cancellation_date"])

    def test_ai_endpoints_need_a_plan_only_when_switched_on(self):
        fake_groq = patch("app.services.instagram_service.groq_caption", new=AsyncMock(return_value={"code": 200}))
        fake_groq.start()
        self.addCleanup(fake_groq.stop)
        anonymous = self.client.post("/groq_caption", data={"text": "x"})
        self.assertEqual(anonymous.status_code, 200)  # switch off: unchanged for old apps

        with patch.object(settings, "ai_requires_subscription", True):
            self.assertEqual(self.client.post("/groq_caption", data={"text": "x"}).status_code, 401)
            guest = self.client.post("/groq_caption", data={"text": "x"}, headers={"Authorization": f"Bearer {self.guest()}"})
            self.assertEqual(guest.json()["data"], {"requires_login": True})

            _, other = self.account(email="other@example.com", auth_user_id="7")
            response = self.client.post("/groq_caption", data={"text": "x"}, headers={"Authorization": f"Bearer {other}"})
            self.assertEqual((response.status_code, response.json()["data"]), (402, {"requires_subscription": True}))

            ok = self.client.post("/groq_caption", data={"text": "x"}, headers={"Authorization": f"Bearer {self.token}"})
            self.assertEqual(ok.status_code, 200)


if __name__ == "__main__":
    unittest.main()
