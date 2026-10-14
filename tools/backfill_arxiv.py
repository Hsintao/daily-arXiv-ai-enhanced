"""Backfill arXiv papers for a date range into the crawl format.

Why this exists: the daily spider only reads https://arxiv.org/list/{cat}/new,
which is a single-day view. When this fork's data branch started empty and
later gaps needed filling, there was no way to recover history with the
existing code.

This script queries the arXiv API by submittedDate instead, and emits records
in exactly the same shape as daily_arxiv/daily_arxiv/spiders/arxiv.py so the
normal enhance.py pipeline can consume them unchanged:

    {"id", "categories", "pdf", "abs", "authors", "title", "comment", "summary"}

It writes one file per date: <outdir>/<YYYY-MM-DD>.jsonl

Usage:
    python backfill_arxiv.py --start 2026-09-07 --end 2026-10-13 \
        --categories cs.CV,cs.CL --outdir ../data

The arXiv API is rate limited and asks clients to be polite; requests are
spaced out and results are cached per day so a re-run does not refetch.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

API = "https://export.arxiv.org/api/query"

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"

# arXiv asks for <= 1 request every 3 seconds. Measured: a steady 5s gap
# returns 200 consistently, while rapid-fire requests trip a 429 that
# persists for a while. Being generous here is much cheaper than getting
# throttled mid-run.
REQUEST_DELAY_SECONDS = 5.0
# The API caps max_results at 2000 but paginating in smaller pages is far more
# reliable and lets us show progress.
PAGE_SIZE = 100
MAX_PER_DAY = 2000


def fetch_page(query: str, start: int, page_size: int) -> str:
    """Fetch one page of the arXiv API, retrying with backoff on 429s."""
    params = urllib.parse.urlencode(
        {
            "search_query": query,
            "start": start,
            "max_results": page_size,
            "sortBy": "submittedDate",
            "sortOrder": "ascending",
        }
    )
    url = f"{API}?{params}"

    last_error = None
    for attempt in range(6):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return response.read().decode("utf-8")
        except Exception as error:  # noqa: BLE001 - network layer is broad
            last_error = error
            # Back off hard: a 429 means we are already over the limit, so
            # retrying quickly (as an earlier version did) only deepens it.
            wait = REQUEST_DELAY_SECONDS * (2 ** attempt)
            wait = min(wait, 120)
            print(
                f"    request failed ({error}); backing off {wait:.0f}s "
                f"(attempt {attempt + 1}/6)",
                file=sys.stderr,
            )
            time.sleep(wait)

    raise RuntimeError(f"Giving up on {url}: {last_error}")


def parse_entries(xml_text: str):
    """Yield records from an Atom feed in the spider's exact shape."""
    root = ET.fromstring(xml_text)

    total = root.find(f"{OPENSEARCH}totalResults")
    total = int(total.text) if total is not None and total.text else 0

    records = []
    for entry in root.findall(f"{ATOM}entry"):
        raw_id = entry.findtext(f"{ATOM}id") or ""
        # http://arxiv.org/abs/2609.09155v1 -> 2609.09155
        arxiv_id = raw_id.rsplit("/", 1)[-1]
        if "v" in arxiv_id:
            arxiv_id = arxiv_id.rsplit("v", 1)[0]

        title = (entry.findtext(f"{ATOM}title") or "").strip()
        summary = (entry.findtext(f"{ATOM}summary") or "").strip()
        # arXiv wraps long titles/summaries across lines.
        title = " ".join(title.split())
        summary = " ".join(summary.split())

        authors = [
            (a.findtext(f"{ATOM}name") or "").strip()
            for a in entry.findall(f"{ATOM}author")
        ]

        comment_el = entry.find(f"{ARXIV}comment")
        comment = comment_el.text.strip() if comment_el is not None and comment_el.text else None

        categories = [
            c.attrib.get("term", "")
            for c in entry.findall(f"{ATOM}category")
            if c.attrib.get("term")
        ]

        # Date the paper was announced (not the v1 submission date), which is
        # what the /new listing would have shown that day.
        published = entry.findtext(f"{ATOM}published") or ""
        updated = entry.findtext(f"{ATOM}updated") or ""

        records.append(
            {
                "id": arxiv_id,
                "categories": categories,
                "pdf": f"https://arxiv.org/pdf/{arxiv_id}",
                "abs": f"https://arxiv.org/abs/{arxiv_id}",
                "authors": authors,
                "title": title,
                "comment": comment,
                "summary": summary,
                "_published": published,
                "_updated": updated,
            }
        )

    return total, records


def fetch_day(day: date, categories: list) -> list:
    """Fetch every paper announced on `day` in the given categories."""
    cat_clause = " OR ".join(f"cat:{c}" for c in categories)
    # Use a generous window: arXiv's 'published' timestamp is UTC and listings
    # roll over, so query a 24h band and filter precisely afterwards.
    start = f"{day.strftime('%Y%m%d')}0000"
    end = f"{day.strftime('%Y%m%d')}2359"
    query = f"({cat_clause}) AND submittedDate:[{start} TO {end}]"

    print(f"  query: {day} ({len(categories)} categories)", file=sys.stderr)

    collected = []
    seen = set()
    start_index = 0

    while start_index < MAX_PER_DAY:
        xml_text = fetch_page(query, start_index, PAGE_SIZE)
        total, records = parse_entries(xml_text)

        if not records:
            break

        for record in records:
            if record["id"] in seen:
                continue
            seen.add(record["id"])

            # Keep only papers whose announcement date matches the target day.
            stamp = record["_published"] or record["_updated"]
            if stamp:
                try:
                    published = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    if published.astimezone(timezone.utc).date() != day:
                        continue
                except ValueError:
                    pass

            # Mirror the spider: keep papers matching any target category.
            if categories and not set(record["categories"]) & set(categories):
                continue

            collected.append(record)

        start_index += PAGE_SIZE
        if start_index >= total:
            break
        time.sleep(REQUEST_DELAY_SECONDS)

    # Drop the internal bookkeeping fields before writing.
    for record in collected:
        record.pop("_published", None)
        record.pop("_updated", None)

    return collected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="YYYY-MM-DD (inclusive)")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    parser.add_argument("--categories", default="cs.CV,cs.CL")
    parser.add_argument("--outdir", required=True)
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Do not refetch days whose output file already has content",
    )
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    categories = [c.strip() for c in args.categories.split(",") if c.strip()]

    if start > end:
        print("start must be <= end", file=sys.stderr)
        return 2

    os.makedirs(args.outdir, exist_ok=True)

    total_days = (end - start).days + 1
    written = skipped = empty = 0
    failures = []

    for offset in range(total_days):
        day = start + timedelta(days=offset)
        target = os.path.join(args.outdir, f"{day.isoformat()}.jsonl")

        if args.skip_existing and os.path.exists(target) and os.path.getsize(target) > 0:
            print(f"[{offset + 1}/{total_days}] {day} -> already present, skipping")
            skipped += 1
            continue

        print(f"[{offset + 1}/{total_days}] {day} -> fetching")
        try:
            records = fetch_day(day, categories)
        except Exception as error:  # noqa: BLE001
            print(f"    FAILED: {error}", file=sys.stderr)
            failures.append((day.isoformat(), str(error)))
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        if not records:
            # Weekends and holidays legitimately have no announcements.
            print(f"    no papers announced (weekend/holiday?)")
            empty += 1
        else:
            with open(target, "w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"    wrote {len(records)} papers -> {target}")
            written += 1

        time.sleep(REQUEST_DELAY_SECONDS)

    print()
    print(f"days with data : {written}")
    print(f"days skipped   : {skipped}")
    print(f"days empty     : {empty}")
    print(f"failures       : {len(failures)}")
    for day, error in failures:
        print(f"  {day}: {error}")

    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
