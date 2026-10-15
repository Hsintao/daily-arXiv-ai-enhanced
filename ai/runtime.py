"""Runtime configuration shared by the paper-enhancement job."""

import os
import random
import re
import time
from urllib.parse import urlparse

# Retry policy for transient provider failures.
#
# The daily job runs ~1000 papers through 8 concurrent workers against a
# provider that rate-limits. A single HTTP 429 / 5xx / timeout used to be
# fatal for the whole batch (see raise_if_processing_failed below), throwing
# away every summary generated up to that point. These defaults are
# deliberately conservative: 4 attempts, 2s base delay, exponential growth
# capped at 30s, so the worst case per paper is ~46s of waiting instead of a
# dead 10-minute run.
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 2.0
DEFAULT_MAX_DELAY = 30.0
# Fraction of the delay randomly added/subtracted, so the 8 workers that all
# hit a 429 at the same instant do not retry in lockstep (thundering herd).
DEFAULT_JITTER = 0.5

# Hard upper bounds. These exist because the policy is operator-tunable through
# repository Variables: a typo such as AI_MAX_RETRIES=999999 would otherwise
# turn a 10-minute job into one that hangs until GitHub's 6-hour runner timeout
# while burning provider quota. Bounding the knobs keeps the worst case
# predictable no matter what the environment says.
#
# Worst case per paper at the limits below (11 attempts, 300s ceiling, +100%
# jitter): 10 * 300s * 2 = 6000s = 100 minutes. That is the hard ceiling the
# environment can produce; it is bounded, and the default policy (4 attempts,
# 2s base, 30s ceiling, 0.5 jitter) waits ~46s per paper instead.
MAX_RETRIES_LIMIT = 10
MAX_DELAY_LIMIT = 300.0
# Guards retry_delays() against building an enormous list even if a caller
# passes a huge count directly (it is a test/logging helper, not the hot path).
MAX_SCHEDULE_LENGTH = 64
# Absolute ceiling the env can produce, derived from the limits above. Exposed
# so tests can assert the bound instead of hard-coding it.
MAX_WORST_CASE_PAPER_WAIT = MAX_RETRIES_LIMIT * MAX_DELAY_LIMIT * 2.0

# Provider messages that mean "come back later" even when no HTTP status is
# available (langchain/httpx sometimes only surfaces str(error)).
_RETRYABLE_MESSAGE_MARKERS = (
    "too many requests",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "connection error",
    "connect error",
    "server disconnected",
    "remoteprotocolerror",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "overloaded",
    "try again",
    "stream error",
)

# A 429 quoted in prose ("received 429", "got 429") is a real rate limit, but a
# 429 embedded in a longer number ("paper 1429", "id 14290") is not.
_RETRYABLE_PROSE_429_PATTERN = re.compile(r"(?<!\d)429(?!\d)")

# Status codes are only trusted when the message actually presents them as an
# HTTP status ("Error code: 429", "HTTP 503"), never as a bare number -- a bare
# "500" appears in unrelated text such as token counts or paper ids.
_RETRYABLE_STATUS_PATTERN = re.compile(
    r"(?:error\s*code|status(?:\s*code)?|http)\s*[:\s]\s*(429|5\d\d)\b",
    re.IGNORECASE,
)

# Provider messages that mean "retrying cannot help". Checked BEFORE the
# retryable markers, because e.g. an auth error body can echo "429".
_PERMANENT_MESSAGE_MARKERS = (
    "insufficient balance",
    "insufficient_quota",
    "quota exhausted",
    "exceeded your current quota",
    "invalid api key",
    "incorrect api key",
    "authentication",
    "unauthorized",
    "forbidden",
    "context length",
    "context_length_exceeded",
    "maximum context",
    "too many tokens",
    "model not found",
    "model_not_found",
    "does not exist",
    # A malformed structured response is handled by its own recovery branch in
    # enhance.py; retrying it just burns tokens.
    "outputparser",
    "validationerror",
    "validation error",
)

# Permanent HTTP statuses, matched only in an explicit "status/code" context.
# 429 is deliberately excluded: it is a permanent-status-shaped number that is
# actually retryable, and must fall through to _RETRYABLE_STATUS_PATTERN.
_PERMANENT_STATUS_PATTERN = re.compile(
    r"(?:error\s*code|status(?:\s*code)?|http)\s*[:\s]\s*(4(?!29)\d\d)\b",
    re.IGNORECASE,
)

# Exception class names worth retrying, matched against the MRO. Names are used
# instead of imports so this module stays importable without openai/httpx.
_RETRYABLE_EXCEPTION_NAMES = frozenset(
    {
        "APITimeoutError",
        "APIConnectionError",
        "RateLimitError",
        "InternalServerError",
        "APIStatusError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "ReadError",
        "WriteTimeout",
        "WriteError",
        "PoolTimeout",
        "RemoteProtocolError",
        "TimeoutException",
        "Timeout",
        "ConnectionError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "IncompleteReadError",
        "OSError",
        "ServiceUnavailableError",
    }
)

# Exception class names that must never be retried. OutputParserException and
# ValidationError live here because enhance.py recovers from them itself.
_PERMANENT_EXCEPTION_NAMES = frozenset(
    {
        "AuthenticationError",
        "PermissionDeniedError",
        "NotFoundError",
        "BadRequestError",
        "UnprocessableEntityError",
        "OutputParserException",
        "ValidationError",
    }
)


def _exception_names(error: BaseException) -> set:
    """Return every class name in the exception's MRO."""
    return {cls.__name__ for cls in type(error).__mro__}


def _exception_status(error: BaseException):
    """Best-effort HTTP status code from openai/httpx style exceptions."""
    for attribute in ("status_code", "http_status", "code"):
        value = getattr(error, attribute, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None


def is_retryable_error(error: BaseException) -> bool:
    """Decide whether a provider failure is worth retrying.

    Retryable: HTTP 429, HTTP 5xx, timeouts, connection resets/errors and
    provider "overloaded"/"busy" messages. Non-retryable: auth failures,
    invalid API keys, exhausted balance/quota, unknown model/404 endpoint,
    context-length overflow, and parse/validation errors.
    """
    names = _exception_names(error)
    text = f"{type(error).__name__}: {error}".lower()

    # "insufficient balance" is reported by DeepSeek/Ark with an HTTP 402/400
    # and, in some SDK paths, as a generic APIStatusError -- so the message
    # check must win over the class/status checks.
    if any(marker in text for marker in _PERMANENT_MESSAGE_MARKERS):
        return False

    status = _exception_status(error)
    if status is not None:
        if status == 429 or 500 <= status < 600:
            return True
        if 400 <= status < 500:
            return False

    # Some SDK paths lose the HTTP status and only keep it in the message. The
    # status is only trusted in an explicit "Error code: 503" style phrase, so
    # unrelated digits (token counts, paper ids) cannot flip the decision.
    if _PERMANENT_STATUS_PATTERN.search(text):
        return False

    if names & _PERMANENT_EXCEPTION_NAMES:
        return False

    if names & _RETRYABLE_EXCEPTION_NAMES:
        return True

    if _RETRYABLE_STATUS_PATTERN.search(text):
        return True

    if any(marker in text for marker in _RETRYABLE_MESSAGE_MARKERS):
        return True

    return bool(_RETRYABLE_PROSE_429_PATTERN.search(text))


def _env_int(
    name: str, default: int, minimum: int = 0, maximum: int = None
) -> int:
    """Read an int env var; invalid input falls back, too-large input clamps.

    A too-large value is clamped rather than ignored: an operator who typed
    ``AI_MAX_RETRIES=999999`` meant "retry a lot", and silently reverting to the
    default would be more surprising than honouring the sane upper bound. What
    must never happen is an unbounded value reaching the retry loop.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < minimum:
        return default
    if maximum is not None and value > maximum:
        return maximum
    return value


def _env_float(
    name: str, default: float, minimum: float = 0.0, maximum: float = None
) -> float:
    """Read a float env var; invalid input falls back, too-large input clamps."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    # inf/nan would poison every downstream delay computation.
    if value != value or value in (float("inf"), float("-inf")):
        return default
    if value < minimum:
        return default
    if maximum is not None and value > maximum:
        return maximum
    return value


def retry_config() -> dict:
    """Read the retry policy from the environment (same style as MAX_TOKENS).

    Every field is bounded, because these values are operator-editable
    repository Variables: an unbounded ``AI_MAX_RETRIES`` (e.g. a typo of
    ``999999``) would turn a 10-minute job into one that hangs until GitHub's
    6-hour runner timeout while burning provider quota. A CI job must fail fast
    and predictably, so the policy is clamped to a bounded worst case.
    """
    max_retries = _env_int(
        "AI_MAX_RETRIES",
        DEFAULT_MAX_ATTEMPTS - 1,
        minimum=0,
        maximum=MAX_RETRIES_LIMIT,
    )
    base_delay = _env_float(
        "AI_RETRY_BASE_DELAY",
        DEFAULT_BASE_DELAY,
        minimum=0.0,
        maximum=MAX_DELAY_LIMIT,
    )
    max_delay = _env_float(
        "AI_RETRY_MAX_DELAY",
        DEFAULT_MAX_DELAY,
        minimum=0.0,
        maximum=MAX_DELAY_LIMIT,
    )
    # A delay ceiling below the base delay would make the backoff meaningless;
    # raise the ceiling so the schedule still grows.
    if max_delay < base_delay:
        max_delay = base_delay
    jitter = _env_float(
        "AI_RETRY_JITTER",
        DEFAULT_JITTER,
        minimum=0.0,
        maximum=1.0,
    )
    return {
        # AI_MAX_RETRIES counts *additional* attempts after the first one.
        "max_retries": max_retries,
        "base_delay": base_delay,
        "max_delay": max_delay,
        "jitter": jitter,
    }


def _growth_is_capped(base_delay: float, max_delay: float, steps: int) -> bool:
    """True once exponential growth can no longer increase the delay.

    Used to stop the doubling early instead of computing ``base_delay *
    2**steps``, which raises OverflowError for large ``steps``.
    """
    try:
        return base_delay <= 0.0 or base_delay * (2.0 ** min(steps, 64)) >= max_delay
    except (OverflowError, ValueError):
        return True


def _capped_delay(steps: int, base_delay: float, max_delay: float) -> float:
    """``min(base_delay * 2**steps, max_delay)`` without ever overflowing."""
    if base_delay <= 0.0:
        return 0.0
    # Doubling cannot grow past max_delay once 2**steps would exceed it, so
    # compute the exact power only while it is small enough to be meaningful.
    grown = base_delay
    for _ in range(max(0, int(steps))):
        if grown >= max_delay:
            return max_delay
        grown *= 2.0
    return min(grown, max_delay)


def retry_delays(
    max_retries: int = DEFAULT_MAX_ATTEMPTS - 1,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    jitter: float = DEFAULT_JITTER,
    random_fn=random.uniform,
) -> list:
    """Exponential backoff schedule (no jitter applied to the returned values).

    Overflow-proof and length-safe for any input: the schedule stops growing
    once ``max_delay`` is reached, so neither the exponent nor the list length
    can explode.
    """
    try:
        count = int(max_retries)
    except (TypeError, ValueError, OverflowError):
        return []
    count = max(0, min(count, MAX_SCHEDULE_LENGTH))
    base_delay = _sane_delay(base_delay, DEFAULT_BASE_DELAY)
    max_delay = _sane_delay(max_delay, DEFAULT_MAX_DELAY)
    return [_capped_delay(attempt, base_delay, max_delay) for attempt in range(count)]


def _sane_delay(value, fallback: float) -> float:
    """Coerce a caller-supplied delay to a finite, non-negative float."""
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if value != value or value in (float("inf"), float("-inf")):
        return fallback
    return value if value > 0.0 else 0.0


def backoff_delay(
    attempt: int,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    jitter: float = DEFAULT_JITTER,
    random_fn=random.uniform,
) -> float:
    """Delay before retry number `attempt` (1-based) with +/- jitter applied.

    Overflow-proof for any ``attempt`` value, including ones so large that
    ``base_delay * 2 ** (attempt - 1)`` could not be represented as a float.
    """
    try:
        steps = int(attempt) - 1
    except (TypeError, ValueError, OverflowError):
        steps = 0
    base_delay = _sane_delay(base_delay, DEFAULT_BASE_DELAY)
    max_delay = _sane_delay(max_delay, DEFAULT_MAX_DELAY)
    jitter = _sane_delay(jitter, DEFAULT_JITTER)
    # Jitter must never scale a delay outside its own bounds.
    jitter = min(jitter, 1.0)

    delay = _capped_delay(steps, base_delay, max_delay)
    if jitter > 0 and delay > 0:
        offset = delay * jitter
        try:
            delay = random_fn(max(0.0, delay - offset), delay + offset)
        except (TypeError, ValueError, OverflowError):
            return delay
    try:
        result = float(delay)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if result != result or result in (float("inf"), float("-inf")):
        return 0.0
    return max(0.0, result)



def _diagnose(error: BaseException, attempts: int, max_attempts: int) -> str:
    """Short, actionable reason shown in the workflow log."""
    text = f"{type(error).__name__}: {error}".lower()
    if "insufficient balance" in text or "insufficient_quota" in text:
        return "provider account has no remaining balance/quota; top up the account"
    if "invalid api key" in text or "incorrect api key" in text:
        return "invalid API key; check OPENAI_API_KEY matches OPENAI_BASE_URL"
    if "authentication" in text or "unauthorized" in text or _exception_status(error) in (401, 403):
        return "authentication failed; check the OPENAI_API_KEY secret"
    if "context length" in text or "context_length_exceeded" in text or "maximum context" in text:
        return "input exceeds the model context window; lower MAX_TOKENS or trim the abstract"
    if "model not found" in text or "does not exist" in text or _exception_status(error) == 404:
        return "model or endpoint not found; check MODEL_NAME and OPENAI_BASE_URL"
    if is_retryable_error(error):
        return (
            f"transient provider failure; gave up after {attempts} attempt(s) "
            f"(max {max_attempts}); consider lowering --max_workers or raising AI_MAX_RETRIES"
        )
    return "permanent provider failure; retrying would not help"


def invoke_with_retry(
    chain,
    payload: dict,
    *,
    max_retries: int = None,
    base_delay: float = None,
    max_delay: float = None,
    jitter: float = None,
    sleep=None,
    on_retry=None,
    random_fn=random.uniform,
):
    """Call ``chain.invoke(payload)`` with bounded retries for transient errors.

    Pure control flow: no provider imports, no global state, safe from many
    threads at once (each call owns its own attempt counter). ``sleep`` is
    injectable so tests never wait, and non-retryable errors raise immediately
    on the first attempt.

    ``sleep`` defaults to ``time.sleep`` resolved *at call time* rather than as
    a default argument, so patching ``runtime.time.sleep`` in a test actually
    takes effect instead of silently sleeping for real.
    """
    if sleep is None:
        sleep = time.sleep
    config = retry_config()
    if max_retries is None:
        max_retries = config["max_retries"]
    if base_delay is None:
        base_delay = config["base_delay"]
    if max_delay is None:
        max_delay = config["max_delay"]
    if jitter is None:
        jitter = config["jitter"]

    max_attempts = max(1, int(max_retries) + 1)

    for attempt in range(1, max_attempts + 1):
        try:
            return chain.invoke(payload)
        except BaseException as error:  # noqa: BLE001 - re-raised below
            retryable = is_retryable_error(error)
            if not retryable or attempt >= max_attempts:
                # Attach the diagnosis to the exception the caller already
                # handles, so raise_if_processing_failed surfaces it.
                try:
                    error.dsh_attempts = attempt
                    error.dsh_max_attempts = max_attempts
                    error.dsh_diagnosis = _diagnose(error, attempt, max_attempts)
                except Exception:
                    pass
                raise
            delay = backoff_delay(
                attempt,
                base_delay=base_delay,
                max_delay=max_delay,
                jitter=jitter,
                random_fn=random_fn,
            )
            if on_retry is not None:
                try:
                    on_retry(attempt, delay, error)
                except Exception:
                    pass
            sleep(delay)

    # Unreachable: every loop iteration either returns or raises.
    raise RuntimeError("retry loop made no attempt")


def describe_failure(item_id: str, error: BaseException) -> str:
    """Build the actionable per-paper failure message used by enhance.py."""
    attempts = getattr(error, "dsh_attempts", 1)
    max_attempts = getattr(error, "dsh_max_attempts", attempts)
    diagnosis = getattr(error, "dsh_diagnosis", None) or _diagnose(error, attempts, max_attempts)
    return (
        f"AI request failed for {item_id} "
        f"[{type(error).__name__}, after {attempts}/{max_attempts} attempt(s)]: {diagnosis}"
    )


def build_chat_openai_kwargs(model_name: str, base_url: str, api_key: str) -> dict:
    """Build provider-safe ChatOpenAI settings from workflow configuration."""
    model_name = model_name.strip()
    base_url = base_url.strip().rstrip("/")
    api_key = api_key.strip()

    missing = [
        name
        for name, value in (
            ("MODEL_NAME", model_name),
            ("OPENAI_BASE_URL", base_url),
            ("OPENAI_API_KEY", api_key),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"Missing required configuration: {', '.join(missing)}")

    kwargs = {
        "model": model_name,
        "base_url": base_url,
        "api_key": api_key,
    }
    # Per-request timeout. The SDK default is 600s, so a hung connection would
    # hold a worker for 10 minutes before our retry policy even sees it; 300s
    # is generous for a structured response capped by MAX_TOKENS.
    kwargs["timeout"] = _env_float(
        "AI_REQUEST_TIMEOUT", 300.0, minimum=1.0, maximum=1800.0
    )
    hostname = urlparse(base_url).hostname or ""
    # These providers default to thinking mode, which rejects the
    # function_calling tool_choice used by with_structured_output.
    if hostname.endswith("volces.com") or hostname.endswith("deepseek.com"):
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    return kwargs


def raise_if_processing_failed(errors: list[str]) -> None:
    """Fail the workflow when every paper in the batch failed to enhance.

    Per-paper failures are skipped by process_all_items; this is reserved for
    the all-failed case, which means something systemic (auth, quota, provider
    outage) and must not be published as an empty day.
    """
    if errors:
        raise RuntimeError(f"{len(errors)} paper(s) failed AI enhancement: {errors[0]}")
