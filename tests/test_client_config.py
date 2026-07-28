import unittest

from fs42stream.client import build_schedule_api_base_url


class ScheduleAPIConfigTests(unittest.TestCase):
    def test_builds_schedule_api_url_from_split_host_and_port(self):
        self.assertEqual(
            build_schedule_api_base_url(scheme="http", host="192.0.2.10", port=4242, base_path=""),
            "http://192.0.2.10:4242",
        )

    def test_builds_schedule_api_url_with_base_path(self):
        self.assertEqual(
            build_schedule_api_base_url(scheme="https", host="fieldstation42.example.test", port=443, base_path="api"),
            "https://fieldstation42.example.test:443/api",
        )

    def test_omits_port_when_blank(self):
        self.assertEqual(
            build_schedule_api_base_url(scheme="https", host="fieldstation42.example.test", port="", base_path="schedule/api"),
            "https://fieldstation42.example.test/schedule/api",
        )


if __name__ == "__main__":
    unittest.main()
