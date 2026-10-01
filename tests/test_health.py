import asyncio
import json
import unittest
from unittest.mock import Mock, patch

from app.api.v1.endpoints.health import health
from app.core.config import settings
from app.services.health_service import readiness_payload


class HealthServiceTests(unittest.TestCase):
    @patch("app.services.health_service.get_connection")
    def test_healthy_payload_checks_database_and_closes_resources(self, get_connection):
        cursor = Mock()
        connection = Mock()
        connection.cursor.return_value = cursor
        get_connection.return_value = connection

        payload = readiness_payload()

        cursor.execute.assert_called_once_with("SELECT 1")
        cursor.fetchone.assert_called_once_with()
        cursor.close.assert_called_once_with()
        connection.close.assert_called_once_with()
        self.assertTrue(payload["status"])
        self.assertTrue(payload["checks"]["database"]["status"])
        self.assertEqual(payload["app"], {
            "name": settings.app_name,
            "environment": settings.app_env,
        })
        self.assertRegex(payload["timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    @patch("app.services.health_service.get_connection", return_value=None)
    def test_unhealthy_payload_returns_false_database_check(self, get_connection):
        payload = readiness_payload()

        get_connection.assert_called_once_with()
        self.assertFalse(payload["status"])
        self.assertFalse(payload["checks"]["database"]["status"])
        self.assertGreaterEqual(payload["checks"]["database"]["response_time_ms"], 0)

    @patch("app.api.v1.endpoints.health.readiness_payload")
    def test_endpoint_returns_200_for_healthy_payload(self, mocked_payload):
        mocked_payload.return_value = {
            "status": True,
            "app": {"name": "Insta Save API", "environment": "production"},
            "checks": {"database": {"status": True, "response_time_ms": 12}},
            "timestamp": "2026-09-21T10:45:32Z",
        }

        response = asyncio.run(health())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.body)["status"])

    @patch("app.api.v1.endpoints.health.readiness_payload")
    def test_endpoint_returns_503_for_unhealthy_payload(self, mocked_payload):
        mocked_payload.return_value = {
            "status": False,
            "app": {"name": "Insta Save API", "environment": "production"},
            "checks": {"database": {"status": False, "response_time_ms": 5002}},
            "timestamp": "2026-09-21T10:45:32Z",
        }

        response = asyncio.run(health())

        self.assertEqual(response.status_code, 503)
        self.assertFalse(json.loads(response.body)["status"])


if __name__ == "__main__":
    unittest.main()
