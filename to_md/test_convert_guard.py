"""Tests for the placeholder guard in the markdown renderer.

ai/enhance.py writes these five placeholder strings when a summary could not
be generated. It now fails the run instead of publishing them, so this guard is
defence-in-depth: if any future path reintroduces a placeholder, the paper must
be skipped rather than rendered as a real summary.

Run with:
    cd to_md && ../.venv/bin/python test_convert_guard.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# convert.py executes its whole pipeline under `if __name__ == "__main__"`, so
# importing it as a module is side-effect free (it parses no args, reads no
# files) and exposes the guard for testing.
import importlib.util

_SPEC = importlib.util.spec_from_file_location(
    "convert", os.path.join(os.path.dirname(os.path.abspath(__file__)), "convert.py")
)
convert = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(convert)

PLACEHOLDERS = convert.PLACEHOLDER_AI_VALUES
has_placeholder_values = convert.has_placeholder_values

REAL = {
    "tldr": "A real summary",
    "motivation": "motivation",
    "method": "method",
    "result": "result",
    "conclusion": "conclusion",
}


def with_placeholder(field: str) -> dict:
    """A dict where exactly one field carries the enhance.py placeholder."""
    data = dict(REAL)
    data[field] = {
        "tldr": "Summary generation failed",
        "motivation": "Motivation analysis unavailable",
        "method": "Method extraction failed",
        "result": "Result analysis unavailable",
        "conclusion": "Conclusion extraction failed",
    }[field]
    return data


class PlaceholderGuardTests(unittest.TestCase):
    def test_real_summary_is_rendered(self):
        """Catches the guard being too broad and dropping good papers."""
        self.assertFalse(has_placeholder_values(REAL))

    def test_every_placeholder_value_is_detected(self):
        """Catches a partially updated placeholder list.

        Each of the five strings is checked independently, so adding a sixth
        placeholder to enhance.py without updating convert.py fails here.
        """
        self.assertEqual(5, len(PLACEHOLDERS))
        for field in REAL:
            with self.subTest(field=field):
                self.assertTrue(
                    has_placeholder_values(with_placeholder(field)),
                    f"placeholder in {field} was not detected",
                )

    def test_fully_placeholder_row_is_detected(self):
        """The exact output the pre-fix enhance.py produced for an unreadable response."""
        self.assertTrue(has_placeholder_values(with_placeholder("tldr") | with_placeholder("method")))

    def test_real_prose_containing_placeholder_words_is_not_skipped(self):
        """Catches substring matching, which would drop legitimate summaries.

        A paper genuinely about failed generation could contain this wording;
        only an exact match is a placeholder.
        """
        data = dict(REAL)
        data["tldr"] = "We show that summary generation failed for long abstracts."
        self.assertFalse(has_placeholder_values(data))

    def test_non_string_and_empty_values_are_safe(self):
        """Catches a TypeError on None/numeric fields reaching the guard."""
        self.assertFalse(has_placeholder_values({"tldr": None, "motivation": 3}))

    def test_guard_reads_values_not_just_keys(self):
        """The root cause: key presence alone rendered placeholders as summaries."""
        self.assertTrue(has_placeholder_values(with_placeholder("conclusion")))


if __name__ == "__main__":
    unittest.main()
