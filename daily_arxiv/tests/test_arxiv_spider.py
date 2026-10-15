"""Tests for the arXiv listing spider.

Two behaviours have each caused production data loss:

1. Section detection: the Replacement section must be found by its link TEXT,
   not by being the last quick-link anchor -- on a day with no Replacement
   section, the old positional logic skipped the Cross-lists section instead,
   silently dropping every cross-listed paper.
2. Batched metadata fetch: one API request per 100 papers instead of one per
   paper (the old pipeline made a 1200-paper day take ~1 hour of 3s-spaced
   requests).

Run with:
    cd daily_arxiv && PYTHONPATH=. ../.venv/bin/python tests/test_arxiv_spider.py
"""

import os
import unittest
from unittest import mock

from scrapy.http import TextResponse

from daily_arxiv.spiders.arxiv import ArxivSpider

URL = "https://arxiv.org/list/cs.CV/new"


def make_spider() -> ArxivSpider:
    """A spider with the production-like category set (env is read in __init__)."""
    with mock.patch.dict(os.environ, {"CATEGORIES": "cs.CV, cs.CL, cs.AI"}):
        return ArxivSpider()


def make_response(html: str, url: str = URL) -> TextResponse:
    return TextResponse(url=url, body=html.encode("utf-8"), encoding="utf-8")


def quick_links(sections) -> str:
    return "".join(
        f'<li><a href="#item{start}">{name}</a></li>' for name, start in sections
    )


def entry(number: int, arxiv_id: str, category: str) -> str:
    return (
        f"<dt><a name='item{number}'>[{number}]</a>"
        f"<a href='/abs/{arxiv_id}' title='Abstract'>arXiv:{arxiv_id}</a></dt>"
        f"<dd><div class='meta'><span class='list-subjects'>"
        f"<span class='primary-subject'>Some Subject ({category})</span>"
        f"</span></div></dd>"
    )


def listing_html(sections, entries) -> str:
    return (
        "<html><body><div id='dlpage'>"
        f"<ul>{quick_links(sections)}</ul>"
        f"<dl id='articles'>{''.join(entries)}</dl>"
        "</div></body></html>"
    )


class ListingExtractionTests(unittest.TestCase):
    def test_replacement_section_is_skipped(self):
        html = listing_html(
            [("New submissions", 0), ("Cross-lists", 2), ("Replacements", 3)],
            [
                entry(1, "2601.00001", "cs.CV"),
                entry(2, "2601.00002", "cs.CL"),
                entry(3, "2601.00003", "cs.CV"),  # a replacement, not new
            ],
        )
        ids = make_spider()._listing_ids(make_response(html))

        self.assertEqual(["2601.00001", "2601.00002"], ids)

    def test_day_without_replacement_section_keeps_cross_lists(self):
        """Regression: positional 'skip after the last anchor' dropped Cross-lists."""
        html = listing_html(
            [("New submissions", 0), ("Cross-lists", 2)],
            [
                entry(1, "2601.00001", "cs.CV"),
                entry(2, "2601.00002", "cs.CL"),
            ],
        )
        ids = make_spider()._listing_ids(make_response(html))

        self.assertEqual(["2601.00001", "2601.00002"], ids)

    def test_replacements_only_listing_yields_nothing(self):
        """The announcement-window shape seen in production: no New/Cross yet."""
        html = listing_html(
            [("Replacements", 0)],
            [entry(1, "2601.00001", "cs.CV")],
        )

        self.assertEqual([], make_spider()._listing_ids(make_response(html)))

    def test_non_target_category_is_skipped(self):
        html = listing_html(
            [("New submissions", 0)],
            [
                entry(1, "2601.00001", "cs.CV"),
                entry(2, "2601.00002", "math.OC"),
            ],
        )

        self.assertEqual(
            ["2601.00001"], make_spider()._listing_ids(make_response(html))
        )

    def test_cross_listed_paper_is_extracted_once_across_pages(self):
        """The same paper appears on every category listing it belongs to."""
        spider = make_spider()
        html = listing_html(
            [("New submissions", 0)],
            [entry(1, "2601.00001", "cs.CV")],
        )

        first = spider._listing_ids(make_response(html))
        second = spider._listing_ids(
            make_response(html, url="https://arxiv.org/list/cs.AI/new")
        )

        self.assertEqual(["2601.00001"], first)
        self.assertEqual([], second)


class FakePaper:
    def __init__(self, arxiv_id):
        self.categories = ["cs.CV"]
        self.authors = []
        self.title = f"title {arxiv_id}"
        self.comment = None
        self.summary = f"summary {arxiv_id}"
        self._id = arxiv_id

    def get_short_id(self):
        return f"{self._id}v1"


class FakeClient:
    """Records the id_list of every Search it is asked to resolve."""

    def __init__(self, missing=()):
        self.calls = []
        self.missing = set(missing)

    def results(self, search):
        self.calls.append(list(search.id_list))
        return [FakePaper(i) for i in search.id_list if i not in self.missing]


class BatchedMetadataFetchTests(unittest.TestCase):
    def test_fetches_in_batches(self):
        spider = make_spider()
        fake = FakeClient()
        spider._client = fake
        ids = [f"2601.{i:05d}" for i in range(250)]

        records = list(spider._fetch_metadata(ids))

        self.assertEqual(250, len(records))
        self.assertEqual([100, 100, 50], [len(call) for call in fake.calls])
        self.assertEqual("2601.00000", records[0]["id"])
        self.assertEqual("summary 2601.00000", records[0]["summary"])

    def test_version_suffix_is_stripped_from_ids(self):
        spider = make_spider()
        spider._client = FakeClient()

        records = list(spider._fetch_metadata(["2601.00001"]))

        self.assertEqual("2601.00001", records[0]["id"])

    def test_missing_ids_are_dropped_with_a_warning(self):
        spider = make_spider()
        spider._client = FakeClient(missing={"2601.00002"})

        with self.assertLogs("arxiv", level="WARNING"):
            records = list(spider._fetch_metadata(["2601.00001", "2601.00002"]))

        self.assertEqual(["2601.00001"], [r["id"] for r in records])

    def test_failed_chunk_is_logged_and_skipped(self):
        spider = make_spider()
        fake = FakeClient()

        def boom(_search):
            raise RuntimeError("api down")

        fake.results = boom
        spider._client = fake

        with self.assertLogs("arxiv", level="ERROR"):
            records = list(spider._fetch_metadata(["2601.00001"]))

        self.assertEqual([], records)


if __name__ == "__main__":
    unittest.main()
