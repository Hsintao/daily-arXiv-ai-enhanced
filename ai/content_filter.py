"""Best-effort content filtering for generated paper summaries.

The filter posts {"text": ...} to a service and expects {"sensitive": bool}.
The endpoint is configurable so this project does not depend on someone
else's private infrastructure:

    FILTER_URL          full endpoint (takes precedence when set)
    FILTER_URL_DISABLED truthy value (1/true/yes/on/disabled) skips filtering
    FILTER_TOKEN        sent as X-Filter-Token when set (for a Worker
                        configured with REQUIRE_TOKEN)

If neither is set, DEFAULT_FILTER_URL is used. Filtering is deliberately
fail-open: a rate limit or outage must never make an otherwise successful
daily crawl publish an empty data set.
"""

import os
import sys

import requests


# Upstream's public endpoint, kept as the default so behaviour is unchanged
# unless the operator configures something else.
DEFAULT_FILTER_URL = "https://spam.dw-dengwei.workers.dev"

_DISABLED_VALUES = {"1", "true", "yes", "on", "disabled"}


def filter_url() -> str:
    """Resolve the configured endpoint, or '' when filtering is disabled."""
    disabled = os.environ.get("FILTER_URL_DISABLED", "").strip().lower()
    if disabled in _DISABLED_VALUES:
        return ""
    return os.environ.get("FILTER_URL", "").strip() or DEFAULT_FILTER_URL


def is_sensitive(content: str) -> bool:
    """Return True only when the filtering service positively flags content."""
    url = filter_url()
    if not url:
        # Disabled explicitly; treat everything as safe. No per-paper logging,
        # which would otherwise spam the log for every single paper.
        return False

    try:
        headers = {}
        token = os.environ.get("FILTER_TOKEN", "").strip()
        if token:
            headers["X-Filter-Token"] = token

        response = requests.post(url, json={"text": content}, timeout=5, headers=headers)
        if response.status_code == 200:
            return bool(response.json().get("sensitive", False))
        print(
            f"Sensitive check unavailable (HTTP {response.status_code}); allowing content",
            file=sys.stderr,
        )
    except requests.RequestException as error:
        print(f"Sensitive check error; allowing content: {error}", file=sys.stderr)
    return False
