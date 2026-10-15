"""Tests for recovering model output from a truncated / malformed structured response.

Two distinct recovery paths are covered:

1. ValidationError recovery (already shipped) -- the model returned valid JSON
   but omitted required fields.
2. OutputParserException recovery -- the model emitted JSON that is *not* valid
   JSON (typically because LaTeX in the abstract introduced an invalid
   backslash escape). This is the live failure: the old code doubled every
   backslash, which corrupted the already-valid escapes, json.loads raised,
   partial_data stayed {} and all five placeholder strings were published.

The salvage logic lives in enhance.recover_partial_ai_fields so it can be
exercised without a live LLM.

NOTE: enhance.py opens template.txt/system.txt via relative paths at import
time, so these tests must run with cwd=ai/ (see the commands in the module
docstring of the task / the project README):
    cd ai && PYTHONPATH=. ../.venv/bin/python tests/test_recovery.py
"""

import json
import unittest
import unittest.mock

import langchain_core.exceptions
from pydantic import ValidationError

import enhance
import runtime
from enhance import recover_partial_ai_fields
from structure import Structure

DEFAULTS = {
    "tldr": "Summary generation failed",
    "motivation": "Motivation analysis unavailable",
    "method": "Method extraction failed",
    "result": "Result analysis unavailable",
    "conclusion": "Conclusion extraction failed",
}


class HermeticTestCase(unittest.TestCase):
    """Base class that keeps every test offline and fast.

    process_single_item is reached through two network calls in production:

    * the content filter, once per item -- without disabling it, a test that
      exercises process_single_item hits the real third-party filter endpoint
      (~2.5s per paper) and the suite fails on a machine with no network;
    * retry backoff -- a retried item would sleep for real.

    Filtering is deliberately fail-open in production, so disabling it here
    does not change the logic under test. Sleep is replaced with a recording
    no-op so retries are observed rather than waited out.
    """

    def setUp(self):
        filter_patcher = unittest.mock.patch.dict(
            "os.environ", {"FILTER_URL_DISABLED": "true"}
        )
        filter_patcher.start()
        self.addCleanup(filter_patcher.stop)

        self.retry_sleeps = []
        real_invoke_with_retry = runtime.invoke_with_retry

        def no_sleep_invoke_with_retry(chain, payload, **kwargs):
            kwargs["sleep"] = self.retry_sleeps.append
            return real_invoke_with_retry(chain, payload, **kwargs)

        retry_patcher = unittest.mock.patch.object(
            enhance, "invoke_with_retry", no_sleep_invoke_with_retry
        )
        retry_patcher.start()
        self.addCleanup(retry_patcher.stop)


class RaisingChain:
    """Chain that raises on invoke, mirroring a provider/parser failure."""

    def __init__(self, error):
        self.error = error

    def invoke(self, payload):
        raise self.error


class OkChain:
    """Chain that returns a valid Structure, mirroring a healthy paper."""

    def __init__(self, fields):
        self.fields = fields

    def invoke(self, payload):
        return Structure(**self.fields)


def recover(error: ValidationError) -> dict:
    """Mirror of the ValidationError branch in enhance.process_single_item."""
    partial = {}
    for item in error.errors():
        value = item.get("input")
        if isinstance(value, dict):
            partial.update(value)
    return {**DEFAULTS, **partial}, partial


def langchain_error(arguments: str) -> str:
    """Build the exact OutputParserException text LangChain emits.

    Shaped after langchain_core's function-calling parser:
    '... Function Structure arguments: {...} are not valid JSON. Got: ...'
    """
    return (
        "Could not parse tool args for tool 'Structure'.\n"
        "Function Structure arguments: " + arguments + " are not valid JSON. "
        "Received: invalid json"
    )


class TruncatedResponseRecoveryTests(unittest.TestCase):
    def test_partial_response_keeps_model_output_and_fills_the_rest(self):
        """Catches discarding a whole batch because one long abstract truncated.

        This is the live failure: the model returned only `tldr`, validation
        raised, and the run aborted with '1 paper(s) failed AI enhancement'.
        """
        with self.assertRaises(ValidationError) as ctx:
            Structure(tldr="partial summary")

        merged, partial = recover(ctx.exception)

        self.assertEqual(["tldr"], list(partial))
        self.assertEqual("partial summary", merged["tldr"])
        # Every field must be present so downstream conversion cannot KeyError.
        self.assertEqual(set(DEFAULTS), set(merged))
        self.assertEqual(DEFAULTS["motivation"], merged["motivation"])

    def test_long_values_are_recovered_untruncated(self):
        """Catches reading str(e), which elides long values as 'abc...xyz'.

        Only str(ValidationError) truncates; errors()[i]['input'] does not, so
        publishing the elided text would put '...' into the rendered site.
        """
        long_tldr = "知识图谱引导的多任务预训练方法" * 10

        with self.assertRaises(ValidationError) as ctx:
            Structure(tldr=long_tldr)

        merged, _ = recover(ctx.exception)

        self.assertEqual(long_tldr, merged["tldr"])
        self.assertNotIn("...", merged["tldr"])

    def test_multi_error_input_is_merged_not_overwritten(self):
        """Catches keeping only the first error's payload."""
        with self.assertRaises(ValidationError) as ctx:
            Structure(tldr="a", method="b")

        merged, partial = recover(ctx.exception)

        self.assertEqual({"tldr": "a", "method": "b"}, partial)
        self.assertEqual("b", merged["method"])

    def test_recovery_preserves_every_required_field(self):
        """Catches a schema change silently producing KeyErrors downstream."""
        with self.assertRaises(ValidationError) as ctx:
            Structure(tldr="only tldr")

        merged, _ = recover(ctx.exception)

        for field in Structure.model_fields:
            self.assertIn(field, merged, f"{field} missing after recovery")


class InvalidJsonSalvageTests(unittest.TestCase):
    """Cases that FAIL against the old blanket `replace('\\\\', '\\\\\\\\')` fix."""

    def test_latex_escape_keeps_literal_text(self):
        """Catches doubling backslashes, which json.loads then rejects.

        The model emitted a raw single backslash for \\mathcal and \\alpha.
        Those are *invalid* JSON escapes, so the salvage has to neutralise them
        -- but the recovered text must still read \\mathcal{L}, not \\\\mathcal{L}.
        """
        arguments = (
            '{"tldr": "We minimize \\mathcal{L} over the dataset", '
            '"motivation": "why", "method": "use \\alpha-divergence", '
            '"result": "better", "conclusion": "done"}'
        )
        # Sanity-check the fixture really is invalid JSON (raw \m, \a escapes).
        with self.assertRaises(json.JSONDecodeError):
            json.loads(arguments)

        recovered = recover_partial_ai_fields(langchain_error(arguments))

        self.assertEqual(5, len(recovered), recovered)
        self.assertEqual("We minimize \\mathcal{L} over the dataset", recovered["tldr"])
        self.assertEqual("use \\alpha-divergence", recovered["method"])
        self.assertNotIn("\\\\", recovered["tldr"])
        self.assertNotIn("\\\\", recovered["method"])

    def test_already_valid_escapes_are_not_corrupted(self):
        """Catches corrupting escapes that were already valid JSON.

        The model correctly emitted \\\\ (an escaped backslash) and \\" (an
        escaped quote) *and* a raw \\omega further along. Blanket doubling turns
        \\\\ into \\\\\\\\ and \\" into \\\\", so even though \\omega needed
        repair, the whole object becomes unparseable and every field is lost.
        """
        arguments = (
            '{"tldr": "path C:\\\\data and a quote: \\"ok\\", \\omega small", '
            '"motivation": "m", "method": "x", "result": "y", "conclusion": "z"}'
        )
        with self.assertRaises(json.JSONDecodeError):
            json.loads(arguments)

        recovered = recover_partial_ai_fields(langchain_error(arguments))

        self.assertEqual(5, len(recovered), recovered)
        # The raw JSON must decode to exactly one backslash and one quote.
        self.assertEqual(
            'path C:\\data and a quote: "ok", \\omega small', recovered["tldr"]
        )

    def test_prose_and_prefix_noise_does_not_truncate_at_first_brace(self):
        """Catches anchoring on LangChain's exact phrasing, and brace mismatches.

        A model that emits prose before/after the object (or a nested object)
        must still yield the *whole* object, not the first fragment.
        """
        arguments = (
            'Sure, here is the JSON you asked for: '
            '{"tldr": "contains {braces} and {\\"nested\\": 1} inline", '
            '"motivation": "m", "method": "x", "result": "y", "conclusion": "z"} '
            'Let me know if you need changes.'
        )
        recovered = recover_partial_ai_fields(langchain_error(arguments))

        self.assertEqual(5, len(recovered), recovered)
        self.assertEqual('contains {braces} and {"nested": 1} inline', recovered["tldr"])

    def test_trailing_comma_before_closing_brace_is_tolerated(self):
        """Catches aborting on the single most common malformed-JSON shape."""
        arguments = (
            '{"tldr": "a", "motivation": "b", "method": "c", '
            '"result": "d", "conclusion": "e",}'
        )
        recovered = recover_partial_ai_fields(langchain_error(arguments))

        self.assertEqual("e", recovered.get("conclusion"), recovered)

    def test_unparseable_input_falls_back_to_empty_dict(self):
        """Catches raising instead of degrading: a bad response must not abort."""
        self.assertEqual({}, recover_partial_ai_fields("not an error message at all"))
        self.assertEqual({}, recover_partial_ai_fields(langchain_error("{not json")))
        self.assertEqual({}, recover_partial_ai_fields(""))

    def test_recovered_fields_merge_with_defaults_only_when_missing(self):
        """Catches letting a partial payload drop a real model field."""
        arguments = '{"tldr": "kept", "method": "kept too"}'
        recovered = recover_partial_ai_fields(langchain_error(arguments))
        merged = {**DEFAULTS, **recovered}

        self.assertEqual("kept", merged["tldr"])
        self.assertEqual("kept too", merged["method"])
        self.assertEqual(DEFAULTS["motivation"], merged["motivation"])
        self.assertEqual(set(DEFAULTS), set(merged))


class UnrecoverableResponseFailsThePaperTests(HermeticTestCase):
    """An unrecoverable response must FAIL, not publish placeholder text.

    to_md/convert.py checks key PRESENCE only, so a placeholder-filled dict
    renders as a real summary on the live site while the batch reports success
    -- the exact outcome raise_if_processing_failed exists to prevent.
    """

    def test_output_parser_exception_recovering_nothing_fails_the_paper(self):
        """Catches publishing 5 placeholders and reporting SUCCESS."""
        chain = RaisingChain(
            langchain_core.exceptions.OutputParserException(
                "Function Structure arguments: {totally broken are not valid JSON"
            )
        )

        with self.assertRaises(RuntimeError) as ctx:
            enhance.process_single_item(chain, {"id": "2610.14332", "summary": "a"}, "Chinese")

        message = str(ctx.exception)
        self.assertIn("2610.14332", message)
        # Reuses describe_failure so the contract matches the retry path.
        self.assertIn("AI request failed for 2610.14332", message)
        self.assertIn("recovered no fields", message)

    def test_output_parser_exception_does_not_return_placeholder_ai(self):
        """Catches the paper sneaking through as a dict of placeholders."""
        chain = RaisingChain(
            langchain_core.exceptions.OutputParserException(
                "Function Structure arguments: {totally broken are not valid JSON"
            )
        )
        item = {"id": "2610.14332", "summary": "a"}

        with self.assertRaises(RuntimeError):
            enhance.process_single_item(chain, item, "Chinese")

        # Nothing may be published for this paper.
        self.assertNotIn("AI", item)

    def test_validation_error_with_no_usable_input_fails_the_paper(self):
        """Catches a ValidationError whose inputs are all empty dicts passing."""
        with self.assertRaises(ValidationError) as vctx:
            Structure()  # every error carries input={}

        chain = RaisingChain(vctx.exception)

        with self.assertRaises(RuntimeError) as ctx:
            enhance.process_single_item(chain, {"id": "1234.5678", "summary": "a"}, "Chinese")

        message = str(ctx.exception)
        self.assertIn("1234.5678", message)
        self.assertIn("AI request failed for 1234.5678", message)
        self.assertIn("recovered no fields", message)

    def test_recoverable_validation_error_still_succeeds(self):
        """Guards against over-correcting: partial output must still be kept."""
        with self.assertRaises(ValidationError) as vctx:
            Structure(tldr="partial summary")

        item = {"id": "1234.5678", "summary": "a"}
        out = enhance.process_single_item(RaisingChain(vctx.exception), item, "Chinese")

        self.assertIsNotNone(out)
        self.assertEqual("partial summary", out["AI"]["tldr"])
        # Missing fields are still default-filled (existing behavior preserved).
        self.assertEqual(DEFAULTS["motivation"], out["AI"]["motivation"])
        self.assertEqual(set(DEFAULTS), set(out["AI"]))

    def test_recoverable_output_parser_error_still_succeeds(self):
        """Guards against over-correcting: salvageable JSON must still be kept."""
        arguments = (
            '{"tldr": "We minimize \\mathcal{L}", "motivation": "why", '
            '"method": "use \\alpha", "result": "better", "conclusion": "done"}'
        )
        chain = RaisingChain(
            langchain_core.exceptions.OutputParserException(langchain_error(arguments))
        )

        item = {"id": "2401.00001", "summary": "a"}
        out = enhance.process_single_item(chain, item, "Chinese")

        self.assertIsNotNone(out)
        self.assertEqual("We minimize \\mathcal{L}", out["AI"]["tldr"])
        self.assertNotIn("\\\\", out["AI"]["tldr"])

    def test_batch_with_one_unrecoverable_paper_fails_the_run(self):
        """End-to-end: a healthy batch plus one bad paper must not report success.

        This is the release-blocking behavior: the paper is routed into
        processing_errors, so raise_if_processing_failed raises
        'N paper(s) failed AI enhancement'.
        """
        broken = langchain_core.exceptions.OutputParserException(
            "Function Structure arguments: {totally broken are not valid JSON"
        )
        partial_error = langchain_core.exceptions.OutputParserException(
            langchain_error('{"tldr": "salvaged"}')
        )
        data = [
            {"id": "healthy.1", "summary": "a"},
            {"id": "2610.14332", "summary": "b"},
            {"id": "healthy.2", "summary": "c"},
        ]

        chains = {
            "healthy.1": OkChain({"tldr": "t1", "motivation": "m", "method": "x", "result": "r", "conclusion": "c"}),
            "2610.14332": RaisingChain(broken),
            "healthy.2": RaisingChain(partial_error),
        }

        original = enhance.process_single_item
        # process_all_items() constructs a real ChatOpenAI before dispatching.
        # Patch that out (and supply placeholder config) so this test exercises
        # only the batch/error-routing logic and never touches the network or
        # depends on proxy settings of the machine running the suite.
        env = {
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": "https://api.deepseek.com/v1",
            "MODEL_NAME": "deepseek-flash",
        }
        try:
            enhance.process_single_item = (
                lambda chain, item, language: original(chains[item["id"]], item, language)
            )
            with unittest.mock.patch.dict("os.environ", env), \
                    unittest.mock.patch.object(enhance, "ChatOpenAI"):
                with self.assertRaises(RuntimeError) as ctx:
                    enhance.process_all_items(data, "deepseek-flash", "Chinese", 1)
        finally:
            enhance.process_single_item = original

        self.assertRegex(
            str(ctx.exception), r"\d+ paper\(s\) failed", str(ctx.exception)
        )
        self.assertIn("1 paper(s) failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
