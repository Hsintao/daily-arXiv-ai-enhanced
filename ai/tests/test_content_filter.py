import os
import unittest
from unittest.mock import patch

from content_filter import DEFAULT_FILTER_URL, filter_url, is_sensitive


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.payload = payload or {}

    def json(self):
        return self.payload


class FilterUrlTests(unittest.TestCase):
    """The endpoint must be configurable to avoid depending on a third party."""

    def setUp(self):
        self._saved = {
            k: os.environ.get(k) for k in ("FILTER_URL", "FILTER_URL_DISABLED")
        }
        os.environ.pop("FILTER_URL", None)
        os.environ.pop("FILTER_URL_DISABLED", None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_defaults_to_upstream_endpoint(self):
        """Catches changing default behaviour for operators who configure nothing."""
        self.assertEqual(DEFAULT_FILTER_URL, filter_url())

    def test_custom_url_takes_precedence(self):
        """Catches the self-hosted endpoint being ignored."""
        os.environ["FILTER_URL"] = "https://my-worker.example.workers.dev"

        self.assertEqual("https://my-worker.example.workers.dev", filter_url())

    def test_disabled_skips_filtering(self):
        """Catches FILTER_URL_DISABLED not being honoured."""
        os.environ["FILTER_URL_DISABLED"] = "true"

        self.assertEqual("", filter_url())

    def test_disable_flag_accepts_common_truthy_spellings(self):
        """Catches only accepting the exact string 'true'."""
        for value in ("1", "true", "TRUE", "yes", "on", "disabled"):
            with self.subTest(value=value):
                os.environ["FILTER_URL_DISABLED"] = value
                self.assertEqual("", filter_url())

    def test_false_disable_flag_keeps_filtering_on(self):
        """Catches treating any non-empty value as 'disabled'."""
        os.environ["FILTER_URL_DISABLED"] = "false"

        self.assertEqual(DEFAULT_FILTER_URL, filter_url())

    def test_blank_url_falls_back_to_default(self):
        """Catches an empty configured URL disabling filtering silently."""
        os.environ["FILTER_URL"] = "   "

        self.assertEqual(DEFAULT_FILTER_URL, filter_url())


class SensitiveFilterTests(unittest.TestCase):
    @patch("content_filter.requests.post", return_value=FakeResponse(429))
    def test_service_rate_limit_does_not_block_a_paper(self, _post):
        """Catches the fail-closed branch that silently removed every paper on a 429."""
        self.assertFalse(is_sensitive("A paper abstract"))

    @patch(
        "content_filter.requests.post",
        return_value=FakeResponse(200, {"sensitive": True}),
    )
    def test_confirmed_sensitive_content_is_blocked(self, _post):
        self.assertTrue(is_sensitive("A paper abstract"))

    @patch("content_filter.requests.post")
    def test_disabled_filter_makes_no_request(self, mock_post):
        """Catches a disabled filter still calling out to the network."""
        os.environ["FILTER_URL_DISABLED"] = "true"
        try:
            self.assertFalse(is_sensitive("A paper abstract"))
            mock_post.assert_not_called()
        finally:
            os.environ.pop("FILTER_URL_DISABLED", None)

    @patch(
        "content_filter.requests.post",
        return_value=FakeResponse(200, {"sensitive": False}),
    )
    def test_request_targets_the_configured_url(self, mock_post):
        """Catches posting to a hardcoded endpoint after reconfiguration."""
        os.environ["FILTER_URL"] = "https://my-worker.example.workers.dev"
        try:
            is_sensitive("A paper abstract")
            self.assertEqual(
                "https://my-worker.example.workers.dev",
                mock_post.call_args[0][0],
            )
        finally:
            os.environ.pop("FILTER_URL", None)

    @patch(
        "content_filter.requests.post",
        return_value=FakeResponse(200, {"sensitive": False}),
    )
    def test_token_is_sent_when_configured(self, mock_post):
        """Catches omitting the token a REQUIRE_TOKEN Worker demands."""
        os.environ["FILTER_TOKEN"] = "s3cret"
        try:
            is_sensitive("A paper abstract")
            self.assertEqual(
                "s3cret",
                mock_post.call_args[1]["headers"]["X-Filter-Token"],
            )
        finally:
            os.environ.pop("FILTER_TOKEN", None)

    @patch(
        "content_filter.requests.post",
        return_value=FakeResponse(200, {"sensitive": False}),
    )
    def test_no_auth_header_when_token_absent(self, mock_post):
        """Catches sending a bogus header that a public Worker would reject."""
        os.environ.pop("FILTER_TOKEN", None)

        is_sensitive("A paper abstract")

        self.assertNotIn("X-Filter-Token", mock_post.call_args[1]["headers"])


if __name__ == "__main__":
    unittest.main()
