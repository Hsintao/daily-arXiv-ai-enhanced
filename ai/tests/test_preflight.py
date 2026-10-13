import unittest

from preflight import explain


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


if __name__ == "__main__":
    unittest.main()
