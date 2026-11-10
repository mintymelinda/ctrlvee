import unittest

from src.services.radarr_service import RadarrService


class RadarrServiceTests(unittest.TestCase):
    def test_service_is_unconfigured_without_host_or_api_key(self):
        service = RadarrService(host=None, port=7878, api_key=None)

        self.assertFalse(service.is_configured())
        self.assertIsNone(service.base_url)

    def test_service_builds_http_and_https_base_urls(self):
        http_service = RadarrService(
            host="radarr.local",
            port=7878,
            api_key="test-key",
            use_ssl=False,
        )
        https_service = RadarrService(
            host="radarr.example",
            port=443,
            api_key="test-key",
            use_ssl=True,
        )

        self.assertTrue(http_service.is_configured())
        self.assertEqual(http_service.base_url, "http://radarr.local:7878/api/v3/")
        self.assertEqual(https_service.base_url, "https://radarr.example:443/api/v3/")


if __name__ == "__main__":
    unittest.main()
