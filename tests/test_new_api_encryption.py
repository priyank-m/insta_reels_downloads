"""Encryption is compulsory on the new APIs (/history, /payment except the webhook)."""
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core import client_crypto
from app.core.config import settings
from app.main import create_app


class NewApiEncryptionTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(settings, "client_encryption_key", "client-key-shared-with-the-apps")
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(create_app())

    @staticmethod
    def dec(response):
        return json.loads(client_crypto.decrypt(response.text))

    def test_plain_json_body_is_rejected_with_an_encrypted_400(self):
        for path in ("/history", "/history/import", "/history/delete", "/payment/process/ai-weekly"):
            with self.subTest(path=path):
                response = self.client.post(path, json={"url": "u", "item": {}})
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.headers["content-type"].startswith("text/plain"))
                self.assertEqual(self.dec(response)["message"], "Encrypted request required.")

    def test_bad_ciphertext_is_400(self):
        response = self.client.post("/history", content=b"not-encrypted", headers={"Content-Type": "text/plain"})
        self.assertEqual((response.status_code, self.dec(response)["message"]), (400, "Invalid encrypted request."))
        response = self.client.post("/history", content=client_crypto.encrypt("not json"), headers={"Content-Type": "text/plain"})
        self.assertEqual(response.status_code, 400)

    def test_encrypted_body_passes_and_every_answer_is_encrypted(self):
        # No session token: the endpoint answers 401, and that answer is encrypted too.
        for body in (client_crypto.encrypt(json.dumps({"url": "u"})), json.dumps(client_crypto.encrypt('{"url": "u"}'))):
            response = self.client.post("/history/delete", content=body, headers={"Content-Type": "text/plain"})
            self.assertEqual((response.status_code, self.dec(response)["code"]), (401, 401))

    def test_requests_without_a_body_pass_and_answers_are_encrypted(self):
        for method, path in (("GET", "/history"), ("DELETE", "/history?scope=web"), ("GET", "/payment/my-subscription"),
                             ("POST", "/payment/cancel-subscription")):
            with self.subTest(path=path):
                response = self.client.request(method, path)
                self.assertEqual((response.status_code, self.dec(response)["code"]), (401, 401))

    def test_webhook_stays_plain(self):
        with patch.object(settings, "payment_webhook_key", "k"):
            response = self.client.post("/payment/webhook/wrong", json={"data": {}})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["message"], "Not found.")

    def test_missing_key_is_plain_500(self):
        with patch.object(settings, "client_encryption_key", ""):
            response = self.client.get("/history")
        self.assertEqual((response.status_code, response.json()["code"]), (500, 500))


if __name__ == "__main__":
    unittest.main()
