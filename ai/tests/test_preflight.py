"""Tests for the pre-flight provider diagnostics.

The workflow runs preflight.py *before* a ~1h crawl, so its job is to turn a
provider rejection into the one setting the operator must change. These tests
pin that mapping to the error strings DeepSeek / Volcengine (and OpenAI)
actually return.

    cd ai && PYTHONPATH=. ../.venv/bin/python tests/test_preflight.py
"""

import unittest

from preflight import DIAGNOSTICS, THINKING_DEFAULT_HOSTS, explain

DEEPSEEK_URL = "https://api.deepseek.com/v1"
VOLC_URL = "https://ark.cn-beijing.volces.com/api/v3"
OPENAI_URL = "https://api.openai.com/v1"


class ExplainDiagnosticsTests(unittest.TestCase):
    def test_thinking_mode_conflict_is_explained(self):
        """Catches regressing to a bare stack trace for the outage this guards."""
        advice = explain(
            Exception(
                "Error code: 400 - {'error': {'message': "
                "'Thinking mode does not support this tool_choice'}}"
            )
        )

        self.assertIn("thinking mode", advice)
        self.assertIn("runtime.py", advice)

    def test_authentication_failure_is_explained(self):
        """Catches blaming the model name when the API key is wrong."""
        advice = explain(Exception("Authentication Fails, Your api key is invalid"))

        self.assertIn("API key was rejected", advice)
        self.assertIn("OPENAI_BASE_URL", advice)

    def test_unknown_model_is_explained(self):
        """Catches the stale deepseek-chat model name after DeepSeek's rename."""
        advice = explain(
            Exception(
                "Error code: 400 - {'error': {'message': 'Model Not Exist'}}"
            )
        )

        self.assertIn("MODEL_NAME", advice)

    def test_insufficient_balance_is_explained(self):
        """Catches a billing failure being reported as a connectivity problem."""
        advice = explain(Exception("Error code: 402 - Insufficient Balance"))

        self.assertIn("no remaining credit", advice)

    def test_unrecognised_error_still_gets_guidance(self):
        """Catches returning an empty hint for an unexpected provider error."""
        advice = explain(Exception("some brand new provider failure"))

        self.assertIn("MODEL_NAME", advice)
        self.assertIn("OPENAI_BASE_URL", advice)


class DeepSeekToolChoiceTests(unittest.TestCase):
    """The live DeepSeek/Volc failure: thinking mode vs. forced tool_choice."""

    def test_deepseek_real_tool_choice_400_names_thinking_mode(self):
        """Catches the dead needle: the old entry never matched this text.

        This is the verbatim body DeepSeek returns for a forced tool_choice on
        a thinking model -- it says "does not support this tool_choice", NOT
        "thinking mode does not support", which is why the hint never fired.
        """
        advice = explain(
            Exception(
                "Error code: 400 - {'error': {'message': 'deepseek-reasoner "
                "does not support this tool_choice', 'type': "
                "'invalid_request_error', 'code': 'invalid_request_error'}}"
            ),
            DEEPSEEK_URL,
        )

        self.assertIn("thinking mode", advice.lower())
        self.assertIn("runtime.py", advice)
        self.assertNotIn("Unrecognised", advice)

    def test_volcengine_tool_choice_rejection_names_thinking_mode(self):
        """Catches provider coverage being limited to DeepSeek's wording."""
        advice = explain(
            Exception(
                "Error code: 400 - {'error': {'message': "
                "'Thinking mode does not support this tool_choice', 'code': "
                "'InvalidParameter'}}"
            ),
            VOLC_URL,
        )

        self.assertIn("thinking mode", advice.lower())
        self.assertIn("runtime.py", advice)

    def test_generic_400_on_thinking_host_points_at_thinking_mode(self):
        """Catches a bare 400 being misdiagnosed as a bad model name.

        A thinking/tool_choice rejection frequently surfaces as a generic HTTP
        400 with no useful message. On a thinking-default host the advice must
        lead with thinking mode, not send the operator after MODEL_NAME.
        """
        advice = explain(
            Exception("Error code: 400 - {'error': {'message': 'Invalid request parameters'}}"),
            DEEPSEEK_URL,
        )

        self.assertIn("thinking mode", advice.lower())
        self.assertIn("runtime.py", advice)

    def test_unknown_error_on_thinking_host_is_not_generic_fallback(self):
        """Catches 'Unrecognised provider error' on a host we can still diagnose."""
        advice = explain(Exception("some brand new provider failure"), VOLC_URL)

        self.assertNotIn("Unrecognised provider error", advice)
        self.assertIn("runtime.py", advice)

    def test_reasoning_content_400_is_explained(self):
        """Catches the other real DeepSeek V4 thinking-mode 400 shape."""
        advice = explain(
            Exception(
                "Error code: 400 - {'error': {'message': 'The "
                "reasoning_content in the thinking mode must be passed back "
                "to the API'}}"
            ),
            DEEPSEEK_URL,
        )

        self.assertIn("runtime.py", advice)


class NonThinkingProviderTests(unittest.TestCase):
    """OpenAI must NOT be told to disable thinking -- that advice is wrong there."""

    def test_openai_tool_choice_error_is_not_blamed_on_thinking(self):
        """Catches applying DeepSeek's fix to a provider where thinking is opt-in."""
        advice = explain(
            Exception(
                "Error code: 400 - {'error': {'message': \"Invalid value for "
                "'tool_choice': expected one of auto, none, required\"}}"
            ),
            OPENAI_URL,
        )

        self.assertNotIn("runtime.py", advice)
        self.assertNotIn("thinking mode", advice.lower())

    def test_openai_generic_400_does_not_mention_thinking(self):
        """Catches the generic-400 branch leaking thinking advice to OpenAI."""
        advice = explain(
            Exception("Error code: 400 - {'error': {'message': 'Bad Request'}}"),
            OPENAI_URL,
        )

        self.assertNotIn("thinking mode", advice.lower())

    def test_openai_unknown_error_keeps_the_generic_fallback(self):
        """Catches the thinking-host fallback being applied unconditionally."""
        advice = explain(Exception("some brand new provider failure"), OPENAI_URL)

        self.assertIn("Unrecognised provider error", advice)
        self.assertIn("MODEL_NAME", advice)

    def test_openai_incorrect_api_key_is_explained(self):
        """Catches OpenAI's own auth wording falling through to 'Unrecognised'.

        The table matched DeepSeek's "Authentication Fails" / "invalid api key"
        phrasing only, so OpenAI's real 401 text produced no guidance at all.
        """
        advice = explain(
            Exception(
                "Error code: 401 - {'error': {'message': 'Incorrect API key "
                "provided: sk-...', 'type': 'invalid_request_error'}}"
            ),
            OPENAI_URL,
        )

        self.assertIn("API key was rejected", advice)
        self.assertNotIn("Unrecognised", advice)

    def test_thinking_hosts_match_runtime_configuration(self):
        """Catches this list drifting from build_chat_openai_kwargs' branch."""
        from runtime import build_chat_openai_kwargs

        for url in (DEEPSEEK_URL, VOLC_URL):
            kwargs = build_chat_openai_kwargs("m", url, "k")
            self.assertIn("extra_body", kwargs, url)
            self.assertTrue(
                any(urlparse_host(url).endswith(h) for h in THINKING_DEFAULT_HOSTS),
                f"{url} not covered by THINKING_DEFAULT_HOSTS",
            )
        # ...and OpenAI must not get the thinking override.
        self.assertNotIn(
            "extra_body", build_chat_openai_kwargs("m", OPENAI_URL, "k")
        )


def urlparse_host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).hostname or ""


class DiagnosticsOrderingTests(unittest.TestCase):
    def test_no_diagnostics_entry_shadows_the_thinking_conflict(self):
        """Catches a table needle pre-empting the host-aware thinking advice."""
        from preflight import THINKING_CONFLICT_NEEDLES

        for needle, _ in DIAGNOSTICS:
            for conflict in THINKING_CONFLICT_NEEDLES:
                self.assertNotEqual(
                    needle, conflict, f"{needle!r} handled out of band"
                )

    def test_specific_messages_win_over_the_generic_400_branch(self):
        """Catches '400' swallowing a precise diagnosis.

        The generic-400 branch is applied only after the specific table, so a
        detailed message must produce its own advice on any host.
        """
        advice = explain(
            Exception(
                "Error code: 400 - {'error': {'message': 'Model Not Exist'}}"
            ),
            OPENAI_URL,
        )

        self.assertIn("MODEL_NAME", advice)
        self.assertNotIn("malformed", advice)

    def test_every_needle_is_lowercase(self):
        """Catches needles that can never match str(error).lower()."""
        for needle, _ in DIAGNOSTICS:
            self.assertEqual(needle, needle.lower())

    def test_every_conflict_needle_is_lowercase(self):
        from preflight import THINKING_CONFLICT_NEEDLES

        for needle in THINKING_CONFLICT_NEEDLES:
            self.assertEqual(needle, needle.lower())


if __name__ == "__main__":
    unittest.main()
