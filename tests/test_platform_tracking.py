import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.api.v1.endpoints.instagram import download_media as download_media_endpoint
from app.api.v1.endpoints.instagram import frontend_success as frontend_success_endpoint
from app.main import app
from app.services import instagram_service


class PlatformTrackingTests(unittest.TestCase):
    def test_device_type_validation_accepts_only_ios_or_android(self):
        self.assertIsNone(instagram_service._validate_device_type(None))
        self.assertEqual(instagram_service._validate_device_type(1), 1)
        self.assertEqual(instagram_service._validate_device_type(2), 2)
        with self.assertRaisesRegex(ValueError, "deviceType"):
            instagram_service._validate_device_type(3)

    @patch("app.services.instagram_service.normalize_instagram_url")
    @patch("app.services.instagram_service.log_platform_request")
    def test_download_media_counts_platform_once_before_an_invalid_url_response(self, log_platform, normalize):
        normalize.return_value = {"code": 400, "message": "invalid"}

        response = asyncio.run(instagram_service.download_media(
            instagramURL="not-an-instagram-url",
            deviceId="device-1",
            deviceType=2,
        ))

        self.assertEqual(response["code"], 400)
        log_platform.assert_called_once_with(2)

    @patch("app.services.instagram_service.log_analytics")
    @patch("app.services.instagram_service.update_download_history")
    def test_successful_provider_history_receives_device_type(self, update_history, log_analytics):
        service = instagram_service._instagram_service(
            "test_provider",
            lambda: {"postData": [{"link": "https://example.test/media.jpg"}]},
            require_post_data=True,
        )

        response = instagram_service._run_instagram_service(
            service,
            "device-1",
            "post",
            device_type=1,
        )

        self.assertEqual(response["code"], 200)
        update_history.assert_called_once_with("device-1", True, 1)
        log_analytics.assert_called_once_with("test_provider", "success")

    @patch("app.services.instagram_service.get_connection")
    def test_history_update_preserves_platform_when_optional_value_is_missing(self, get_connection):
        cursor = Mock()
        cursor.fetchone.return_value = (1,)
        connection = Mock()
        connection.cursor.return_value = cursor
        get_connection.return_value = connection

        instagram_service.update_download_history("device-1", True, None)

        update_query, update_values = cursor.execute.call_args_list[-1].args
        self.assertIn("device_type = COALESCE(%s, device_type)", update_query)
        self.assertEqual(update_values[0], None)
        self.assertEqual(update_values[-1], "device-1")

    @patch("app.services.instagram_service.get_connection")
    def test_ios_request_updates_only_ios_counter(self, get_connection):
        cursor = Mock()
        cursor.fetchone.side_effect = [True, True, None]
        connection = Mock()
        connection.cursor.return_value = cursor
        get_connection.return_value = connection

        instagram_service.log_platform_request(1)

        executed_queries = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("`ios_requests` = `ios_requests` + 1" in query for query in executed_queries))
        self.assertFalse(any("`android_requests` = `android_requests` + 1" in query for query in executed_queries))
        connection.commit.assert_called_once_with()

    def test_endpoint_forwards_optional_device_type_to_services(self):
        with patch(
            "app.api.v1.endpoints.instagram.instagram_service.download_media",
            new_callable=AsyncMock,
        ) as download_service, patch(
            "app.api.v1.endpoints.instagram.instagram_service.frontend_success",
            new_callable=AsyncMock,
        ) as frontend_service:
            download_service.return_value = {"code": 200}
            frontend_service.return_value = {"code": 200}

            asyncio.run(download_media_endpoint("url", "device-1", 2))
            asyncio.run(frontend_success_endpoint("device-1", 1))

        download_service.assert_awaited_once_with(
            instagramURL="url",
            deviceId="device-1",
            deviceType=2,
        )
        frontend_service.assert_awaited_once_with(deviceId="device-1", deviceType=1)

    def test_openapi_marks_device_type_optional_and_limited_to_supported_values(self):
        schema = app.openapi()
        for route in ("/download_media", "/frontend_success"):
            content = schema["paths"][route]["post"]["requestBody"]["content"]
            media = next(iter(content.values()))
            body_schema = media["schema"]
            if "$ref" in body_schema:
                body_schema = schema["components"]["schemas"][body_schema["$ref"].split("/")[-1]]

            self.assertNotIn("deviceType", body_schema.get("required", []))
            numeric_schema = body_schema["properties"]["deviceType"]["anyOf"][0]
            self.assertEqual(numeric_schema["minimum"], 1)
            self.assertEqual(numeric_schema["maximum"], 2)


if __name__ == "__main__":
    unittest.main()
