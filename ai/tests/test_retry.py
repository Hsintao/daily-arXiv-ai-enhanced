"""Retry-policy tests for the transient-failure hardening of the AI job.

All fakes: no network, no real sleeping (``sleep`` is injected).
"""

import os
import unittest
from unittest import mock

from runtime import (
    backoff_delay,
    describe_failure,
    invoke_with_retry,
    is_retryable_error,
    retry_config,
    retry_delays,
    DEFAULT_BASE_DELAY,
    DEFAULT_MAX_ATTEMPTS,
    MAX_DELAY_LIMIT,
    MAX_RETRIES_LIMIT,
    MAX_WORST_CASE_PAPER_WAIT,
)


# --------------------------------------------------------------------------
# Fakes that mimic the openai/httpx exception surface without importing SDK
# internals: an HTTP status plus a body message is all the policy reads.
# --------------------------------------------------------------------------
class ProviderError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class RateLimitError(ProviderError):
    """Name matches openai.RateLimitError."""


class InternalServerError(ProviderError):
    """Name matches openai.InternalServerError."""


class AuthenticationError(ProviderError):
    """Name matches openai.AuthenticationError."""


class NotFoundError(ProviderError):
    """Name matches openai.NotFoundError."""


class BadRequestError(ProviderError):
    """Name matches openai.BadRequestError."""


class APIConnectionError(ProviderError):
    """Name matches openai.APIConnectionError."""


class APITimeoutError(ProviderError):
    """Name matches openai.APITimeoutError."""


class FakeChain:
    """Returns queued outcomes in order; each outcome is a value or an error."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def invoke(self, payload):
        self.calls += 1
        if not self.outcomes:
            raise AssertionError("chain invoked more times than expected")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RetryableClassificationTests(unittest.TestCase):
    """Catches a transient provider error being treated as fatal."""

    def test_http_429_is_retryable(self):
        self.assertTrue(is_retryable_error(RateLimitError("rate limit exceeded", 429)))

    def test_http_5xx_is_retryable(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                self.assertTrue(
                    is_retryable_error(InternalServerError("server error", status))
                )

    def test_timeout_is_retryable(self):
        self.assertTrue(is_retryable_error(APITimeoutError("Request timed out.")))
        self.assertTrue(is_retryable_error(TimeoutError("timed out")))

    def test_connection_error_is_retryable(self):
        self.assertTrue(is_retryable_error(APIConnectionError("Connection error.")))
        self.assertTrue(is_retryable_error(ConnectionResetError("Connection reset by peer")))

    def test_provider_overloaded_message_is_retryable(self):
        self.assertTrue(
            is_retryable_error(
                Exception("The engine is currently overloaded, please try again later")
            )
        )

    def test_lowercase_rate_limit_message_is_retryable(self):
        """Catches relying only on exception classes, which providers vary."""
        self.assertTrue(is_retryable_error(Exception("429 Too Many Requests")))
        self.assertTrue(is_retryable_error(Exception("rate limit reached for requests")))

    def test_status_code_in_message_is_retryable(self):
        """Catches SDK paths that lose status_code and keep it only in str(e)."""
        self.assertTrue(is_retryable_error(Exception("Error code: 429 - Too Many Requests")))
        self.assertTrue(is_retryable_error(Exception("HTTP 503 Service Unavailable")))
        self.assertTrue(is_retryable_error(Exception("Error code: 502 - Bad Gateway")))
        self.assertTrue(is_retryable_error(Exception("received 429 from provider")))

    def test_bare_digits_in_unrelated_text_do_not_flip_the_decision(self):
        """Catches a substring match on '429'/'500' in token counts or paper ids."""
        self.assertFalse(
            is_retryable_error(Exception("invalid request for model 500-tokens-limit"))
        )
        self.assertFalse(is_retryable_error(Exception("paper 1429 has an odd id")))
        self.assertFalse(is_retryable_error(Exception("paper id 4290 failed")))


class NonRetryableClassificationTests(unittest.TestCase):
    """Catches retrying a permanent failure 4x and burning CI minutes."""

    def test_authentication_failure_is_not_retryable(self):
        self.assertFalse(
            is_retryable_error(AuthenticationError("401 Unauthorized", 401))
        )
        self.assertFalse(is_retryable_error(Exception("Error code: 401 - Unauthorized")))
        self.assertFalse(is_retryable_error(Exception("HTTP 403 Forbidden")))

    def test_invalid_api_key_message_is_not_retryable(self):
        self.assertFalse(is_retryable_error(Exception("Invalid API key provided")))

    def test_insufficient_balance_is_not_retryable(self):
        """The live DeepSeek failure mode: an empty account, not a rate limit."""
        self.assertFalse(
            is_retryable_error(BadRequestError("Insufficient Balance", 400))
        )
        self.assertFalse(
            is_retryable_error(Exception("You exceeded your current quota"))
        )

    def test_model_not_found_is_not_retryable(self):
        self.assertFalse(
            is_retryable_error(NotFoundError("The model `deepseek-flash` does not exist", 404))
        )
        self.assertFalse(
            is_retryable_error(BadRequestError("model not found: deepseek-flash", 400))
        )

    def test_context_length_exceeded_is_not_retryable(self):
        self.assertFalse(
            is_retryable_error(
                BadRequestError("This model's maximum context length is 65536 tokens", 400)
            )
        )

    def test_plain_bad_request_is_not_retryable(self):
        self.assertFalse(is_retryable_error(BadRequestError("bad payload", 400)))

    def test_parse_and_validation_errors_are_not_retryable(self):
        """They have their own recovery branches in enhance.py."""
        import langchain_core.exceptions
        from pydantic import ValidationError
        from structure import Structure

        self.assertFalse(
            is_retryable_error(
                langchain_core.exceptions.OutputParserException("Could not parse")
            )
        )
        with self.assertRaises(ValidationError) as ctx:
            Structure(tldr="only tldr")
        self.assertFalse(is_retryable_error(ctx.exception))


class BackoffScheduleTests(unittest.TestCase):
    """Catches an unbounded or non-growing backoff schedule."""

    def test_delays_grow_exponentially_and_are_bounded_by_max_delay(self):
        delays = retry_delays(max_retries=6, base_delay=2.0, max_delay=30.0)
        self.assertEqual([2.0, 4.0, 8.0, 16.0, 30.0, 30.0], delays)

    def test_no_retries_means_no_delays(self):
        self.assertEqual([], retry_delays(max_retries=0))

    def test_jitter_keeps_delay_within_bounds(self):
        for attempt in range(1, 6):
            delay = backoff_delay(attempt, base_delay=2.0, max_delay=30.0, jitter=0.5)
            self.assertGreater(delay, 0)
            self.assertLessEqual(delay, 45.0)


class RetryConfigTests(unittest.TestCase):
    """Catches the policy being hard-coded, so operators cannot tune it."""

    def test_defaults_match_documented_values(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            config = retry_config()
        self.assertEqual(DEFAULT_MAX_ATTEMPTS - 1, config["max_retries"])
        self.assertEqual(DEFAULT_BASE_DELAY, config["base_delay"])

    def test_environment_overrides_are_honoured(self):
        env = {
            "AI_MAX_RETRIES": "7",
            "AI_RETRY_BASE_DELAY": "0.5",
            "AI_RETRY_MAX_DELAY": "3",
            "AI_RETRY_JITTER": "0",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            config = retry_config()
        self.assertEqual(7, config["max_retries"])
        self.assertEqual(0.5, config["base_delay"])
        self.assertEqual(3.0, config["max_delay"])
        self.assertEqual(0.0, config["jitter"])

    def test_invalid_environment_values_fall_back_to_defaults(self):
        env = {"AI_MAX_RETRIES": "not-a-number", "AI_RETRY_BASE_DELAY": "-5"}
        with mock.patch.dict(os.environ, env, clear=True):
            config = retry_config()
        self.assertEqual(DEFAULT_MAX_ATTEMPTS - 1, config["max_retries"])
        self.assertEqual(DEFAULT_BASE_DELAY, config["base_delay"])


class UnboundedEnvironmentTests(unittest.TestCase):
    """Catches AI_MAX_RETRIES=999999 hanging CI until the 6-hour timeout.

    This is the HIGH-severity defect: an operator typo in repository Variables
    reached the retry loop unbounded, so one paper could sleep for ~1e6 * 30s.
    """

    PATHOLOGICAL = {
        "AI_MAX_RETRIES": "999999",
        "AI_RETRY_BASE_DELAY": "999999",
        "AI_RETRY_MAX_DELAY": "999999",
        "AI_RETRY_JITTER": "50",
    }

    def test_huge_max_retries_is_clamped_not_accepted(self):
        with mock.patch.dict(os.environ, {"AI_MAX_RETRIES": "999999"}, clear=True):
            config = retry_config()
        self.assertEqual(MAX_RETRIES_LIMIT, config["max_retries"])
        self.assertLess(config["max_retries"], 999999)

    def test_zero_and_negative_max_retries_are_handled(self):
        for raw in ("0", "-1", "-999"):
            with self.subTest(raw=raw):
                with mock.patch.dict(os.environ, {"AI_MAX_RETRIES": raw}, clear=True):
                    config = retry_config()
                # 0 is a legitimate "do not retry"; negatives fall back.
                self.assertGreaterEqual(config["max_retries"], 0)
                self.assertLessEqual(config["max_retries"], MAX_RETRIES_LIMIT)

    def test_garbage_and_non_finite_values_fall_back(self):
        for raw in ("abc", "", "  ", "1e400", "inf", "-inf", "nan", "None"):
            with self.subTest(raw=raw):
                env = {"AI_MAX_RETRIES": raw, "AI_RETRY_JITTER": raw,
                       "AI_RETRY_BASE_DELAY": raw}
                with mock.patch.dict(os.environ, env, clear=True):
                    config = retry_config()
                self.assertEqual(DEFAULT_MAX_ATTEMPTS - 1, config["max_retries"])
                self.assertEqual(DEFAULT_BASE_DELAY, config["base_delay"])
                self.assertLessEqual(config["jitter"], 1.0)
                self.assertGreaterEqual(config["jitter"], 0.0)

    def test_delays_and_jitter_are_clamped_to_sane_ranges(self):
        with mock.patch.dict(os.environ, self.PATHOLOGICAL, clear=True):
            config = retry_config()
        self.assertLessEqual(config["base_delay"], MAX_DELAY_LIMIT)
        self.assertLessEqual(config["max_delay"], MAX_DELAY_LIMIT)
        self.assertGreaterEqual(config["jitter"], 0.0)
        self.assertLessEqual(config["jitter"], 1.0, "jitter > 1 can explode a delay")

    def test_max_delay_is_never_below_base_delay(self):
        """Catches a ceiling below the base making the backoff meaningless."""
        env = {"AI_RETRY_BASE_DELAY": "60", "AI_RETRY_MAX_DELAY": "1"}
        with mock.patch.dict(os.environ, env, clear=True):
            config = retry_config()
        self.assertGreaterEqual(config["max_delay"], config["base_delay"])

    def test_worst_case_per_paper_wait_is_bounded(self):
        """The regression guard: pathological env must not imply an unbounded wait."""
        with mock.patch.dict(os.environ, self.PATHOLOGICAL, clear=True):
            config = retry_config()

        delays = retry_delays(
            max_retries=config["max_retries"],
            base_delay=config["base_delay"],
            max_delay=config["max_delay"],
        )
        # Upper bound: every delay jittered by +jitter.
        worst = sum(d * (1 + config["jitter"]) for d in delays)

        self.assertLessEqual(worst, MAX_WORST_CASE_PAPER_WAIT)
        # Sanity: without the clamp this would have been ~1e6 * 30s = 3e7s+.
        self.assertLess(worst, 3.0e7)

    def test_default_worst_case_wait_is_small(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            config = retry_config()
        worst = sum(
            d * (1 + config["jitter"])
            for d in retry_delays(max_retries=config["max_retries"])
        )
        self.assertLess(worst, 300.0, "default policy should wait well under 5 minutes")


class OverflowSafetyTests(unittest.TestCase):
    """Catches OverflowError from base_delay * 2**attempt for huge inputs."""

    HUGE = (1000, 100000, 10 ** 9, 10 ** 18)

    def test_retry_delays_never_raise_for_huge_counts(self):
        for count in self.HUGE:
            with self.subTest(count=count):
                delays = retry_delays(max_retries=count)
                self.assertTrue(delays)
                # Growth stops at the ceiling instead of exploding the list.
                self.assertLessEqual(len(delays), 64)
                self.assertLessEqual(max(delays), 30.0)

    def test_backoff_delay_never_raises_for_huge_attempts(self):
        for attempt in self.HUGE:
            with self.subTest(attempt=attempt):
                delay = backoff_delay(attempt)
                self.assertGreaterEqual(delay, 0.0)
                self.assertLessEqual(delay, 30.0 * 1.5 + 1e-9)

    def test_delay_functions_never_raise_for_hostile_arguments(self):
        hostile = [
            {"base_delay": 1e308, "max_delay": 1e308},
            {"base_delay": float("inf"), "max_delay": float("inf")},
            {"base_delay": float("nan"), "max_delay": 1.0},
            {"base_delay": -5.0, "max_delay": -1.0},
            {"base_delay": None, "max_delay": None},
            {"base_delay": "abc", "max_delay": "abc"},
        ]
        for kwargs in hostile:
            with self.subTest(kwargs=kwargs):
                delays = retry_delays(max_retries=5, **kwargs)
                self.assertEqual(5, len(delays))
                for d in delays:
                    self.assertGreaterEqual(d, 0.0)
                delay = backoff_delay(3, **kwargs)
                self.assertGreaterEqual(delay, 0.0)

    def test_non_integer_counts_do_not_raise(self):
        for count in (None, "abc", 2.7, float("inf"), float("nan")):
            with self.subTest(count=count):
                delays = retry_delays(max_retries=count)
                self.assertIsInstance(delays, list)

    def test_jitter_above_one_cannot_exceed_its_ceiling(self):
        """Even a caller passing jitter=50 must not blow past the bound."""
        for attempt in (1, 2, 5):
            with self.subTest(attempt=attempt):
                delay = backoff_delay(attempt, jitter=50, random_fn=lambda a, b: b)
                self.assertLessEqual(delay, 30.0 * 2.0 + 1e-9)


class BoundedAttemptsEndToEndTests(unittest.TestCase):
    """The end-to-end proof: a pathological env must terminate, not spin."""

    PATHOLOGICAL = UnboundedEnvironmentTests.PATHOLOGICAL

    def test_pathological_env_terminates_in_a_bounded_number_of_attempts(self):
        chain = FakeChain([RateLimitError("429 Too Many Requests", 429)] * 100000)
        slept = []

        # Patch the env for the WHOLE call: that is how production behaves,
        # since the environment is fixed for the process lifetime.
        with mock.patch.dict(os.environ, self.PATHOLOGICAL, clear=True):
            with self.assertRaises(RateLimitError):
                invoke_with_retry(chain, {}, sleep=slept.append)

        self.assertEqual(MAX_RETRIES_LIMIT + 1, chain.calls)
        self.assertEqual(MAX_RETRIES_LIMIT, len(slept))
        # Without the clamp this would have been ~1e6 attempts.
        self.assertLess(chain.calls, 100)
        self.assertLessEqual(sum(slept), MAX_WORST_CASE_PAPER_WAIT)

    def test_pathological_env_does_not_spin_on_a_permanent_failure(self):
        chain = FakeChain([AuthenticationError("401 Unauthorized", 401)] * 1000)
        slept = []

        with mock.patch.dict(os.environ, self.PATHOLOGICAL, clear=True):
            with self.assertRaises(AuthenticationError):
                invoke_with_retry(chain, {}, sleep=slept.append)

        self.assertEqual(1, chain.calls)
        self.assertEqual([], slept)

    def test_separate_calls_do_not_accumulate_attempts(self):
        """Catches shared mutable state between concurrent workers."""
        with mock.patch.dict(os.environ, self.PATHOLOGICAL, clear=True):
            for _ in range(3):
                chain = FakeChain([RateLimitError("429", 429)] * 100)
                with self.assertRaises(RateLimitError):
                    invoke_with_retry(chain, {}, sleep=lambda _d: None)
                self.assertEqual(MAX_RETRIES_LIMIT + 1, chain.calls)

    def test_retried_paper_under_pathological_env_still_succeeds(self):
        """A bounded retry budget must still let a transient blip recover."""
        chain = FakeChain([RateLimitError("429", 429), RateLimitError("429", 429), "ok"])
        slept = []

        with mock.patch.dict(os.environ, self.PATHOLOGICAL, clear=True):
            result = invoke_with_retry(chain, {}, sleep=slept.append)

        self.assertEqual("ok", result)
        self.assertEqual(3, chain.calls)
        self.assertEqual(2, len(slept))

    def test_default_sleep_is_resolved_at_call_time_not_import_time(self):
        """Catches a test silently sleeping for real.

        If `sleep` were bound as a default argument, patching
        runtime.time.sleep would have no effect and this test would block for
        the full backoff instead of returning immediately.
        """
        import runtime

        chain = FakeChain([RateLimitError("429", 429), "ok"])
        with mock.patch.object(runtime.time, "sleep") as fake_sleep:
            result = invoke_with_retry(chain, {}, max_retries=1)

        self.assertEqual("ok", result)
        fake_sleep.assert_called_once()


class InvokeWithRetryTests(unittest.TestCase):
    """Catches retry logic that never fires, never stops, or retries forever."""

    def setUp(self):
        self.slept = []

    def sleep(self, delay):
        self.slept.append(delay)

    def test_transient_failure_succeeds_on_a_later_attempt(self):
        chain = FakeChain([RateLimitError("429", 429), "ok"])

        result = invoke_with_retry(chain, {"x": 1}, sleep=self.sleep)

        self.assertEqual("ok", result)
        self.assertEqual(2, chain.calls)
        self.assertEqual(1, len(self.slept))
        self.assertGreater(self.slept[0], 0)

    def test_success_on_first_attempt_does_not_sleep(self):
        chain = FakeChain(["ok"])

        self.assertEqual("ok", invoke_with_retry(chain, {}, sleep=self.sleep))
        self.assertEqual([], self.slept)
        self.assertEqual(1, chain.calls)

    def test_attempts_are_bounded_and_no_infinite_loop(self):
        """Catches retrying forever: 3 retries => exactly 4 attempts."""
        chain = FakeChain([RateLimitError("429", 429)] * 10)

        with self.assertRaises(RateLimitError):
            invoke_with_retry(
                chain, {}, max_retries=3, sleep=self.sleep, random_fn=lambda a, b: a
            )

        self.assertEqual(4, chain.calls)
        self.assertEqual(3, len(self.slept))

    def test_permanent_failure_raises_immediately_without_sleeping(self):
        chain = FakeChain([AuthenticationError("401 Unauthorized", 401)] * 5)

        with self.assertRaises(AuthenticationError):
            invoke_with_retry(chain, {}, sleep=self.sleep)

        self.assertEqual(1, chain.calls)
        self.assertEqual([], self.slept)

    def test_insufficient_balance_raises_immediately(self):
        chain = FakeChain([BadRequestError("Insufficient Balance", 400)] * 5)

        with self.assertRaises(BadRequestError):
            invoke_with_retry(chain, {}, sleep=self.sleep)

        self.assertEqual(1, chain.calls)

    def test_permanent_failure_carries_an_actionable_diagnosis(self):
        chain = FakeChain([AuthenticationError("401 Unauthorized", 401)])

        with self.assertRaises(AuthenticationError) as ctx:
            invoke_with_retry(chain, {}, sleep=self.sleep)

        message = describe_failure("2610.14332", ctx.exception)
        self.assertIn("2610.14332", message)
        self.assertIn("AuthenticationError", message)
        self.assertIn("authentication", message.lower())

    def test_exhausted_transient_failure_reports_rate_limited_diagnosis(self):
        chain = FakeChain([RateLimitError("429 Too Many Requests", 429)] * 5)

        with self.assertRaises(RateLimitError) as ctx:
            invoke_with_retry(
                chain, {}, max_retries=2, sleep=self.sleep, random_fn=lambda a, b: a
            )

        message = describe_failure("2610.14332", ctx.exception)
        self.assertIn("2610.14332", message)
        self.assertIn("RateLimitError", message)
        self.assertIn("3 attempt(s)", message)
        self.assertIn("transient", message.lower())

    def test_on_retry_callback_reports_each_retry(self):
        events = []
        chain = FakeChain([RateLimitError("429", 429), "ok"])

        invoke_with_retry(
            chain,
            {},
            sleep=self.sleep,
            on_retry=lambda attempt, delay, error: events.append((attempt, delay, error)),
        )

        self.assertEqual(1, len(events))
        self.assertEqual(1, events[0][0])
        self.assertIsInstance(events[0][2], RateLimitError)

    def test_retry_is_thread_safe_across_concurrent_calls(self):
        from concurrent.futures import ThreadPoolExecutor

        def run(_):
            chain = FakeChain([RateLimitError("429", 429), "ok"])
            return invoke_with_retry(
                chain, {}, sleep=lambda _d: None, random_fn=lambda a, b: a
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(run, range(32)))

        self.assertEqual(["ok"] * 32, results)


class BatchContractTests(unittest.TestCase):
    """Catches breaking the `N paper(s) failed` contract the workflow relies on."""

    def test_permanent_failure_still_raises_the_batch_runtime_error(self):
        from runtime import raise_if_processing_failed

        chain = FakeChain([AuthenticationError("401 Unauthorized", 401)])

        with self.assertRaises(AuthenticationError) as ctx:
            invoke_with_retry(chain, {}, sleep=lambda _d: None)

        failure = describe_failure("2610.14332", ctx.exception)

        with self.assertRaisesRegex(RuntimeError, r"1 paper\(s\) failed"):
            raise_if_processing_failed([failure])

    def test_message_mentions_paper_id_and_exception_type(self):
        chain = FakeChain([RateLimitError("429", 429)] * 9)

        with self.assertRaises(RateLimitError) as ctx:
            invoke_with_retry(chain, {}, max_retries=1, sleep=lambda _d: None)

        with self.assertRaisesRegex(RuntimeError, r"1 paper\(s\) failed") as batch:
            from runtime import raise_if_processing_failed

            raise_if_processing_failed(
                [describe_failure("2610.14332", ctx.exception)]
            )

        self.assertIn("2610.14332", str(batch.exception))
        self.assertIn("RateLimitError", str(batch.exception))


if __name__ == "__main__":
    unittest.main()
