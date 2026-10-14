import unittest

from pydantic import ValidationError

from structure import Structure

DEFAULTS = {
    "tldr": "Summary generation failed",
    "motivation": "Motivation analysis unavailable",
    "method": "Method extraction failed",
    "result": "Result analysis unavailable",
    "conclusion": "Conclusion extraction failed",
}


def recover(error: ValidationError) -> dict:
    """Mirror of the recovery branch in enhance.process_single_item."""
    partial = {}
    for item in error.errors():
        value = item.get("input")
        if isinstance(value, dict):
            partial.update(value)
    return {**DEFAULTS, **partial}, partial


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


if __name__ == "__main__":
    unittest.main()
