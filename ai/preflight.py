"""Pre-flight check for the LLM endpoint used by the enhancement job.

Runs before the (slow, ~1h) crawl + enhancement pipeline so that a
misconfigured provider fails in seconds instead of after a full crawl.

It deliberately reuses the exact same construction path as enhance.py --
build_chat_openai_kwargs(...) + ChatOpenAI(...).with_structured_output(...,
method="function_calling") -- because the failure this guards against
(HTTP 400 "Thinking mode does not support this tool_choice", or DeepSeek's
alternate wording "deepseek-reasoner does not support this tool_choice") only
surfaces when structured output is actually requested. A plain connectivity
ping cannot detect it.

Diagnosis is host-aware: the thinking-mode fix is only offered for hosts that
build_chat_openai_kwargs() actually configures to disable thinking
(volces.com / deepseek.com). Other providers get neutral advice, so an OpenAI
user is never told to edit ai/runtime.py.

Exit codes:
    0  the model answered a structured request successfully
    1  the model is not usable; a human-readable reason was printed
"""

import os
import sys
from urllib.parse import urlparse

from langchain_openai import ChatOpenAI

from runtime import build_chat_openai_kwargs
from structure import Structure

# Minimal prompt: enough to exercise function_calling, small enough to be
# effectively free (a few tokens).
PROBE_SYSTEM = "You are a connectivity probe. Reply using the requested schema."
PROBE_USER = "Summarize this into the schema: The probe checks API connectivity."

# Hosts that build_chat_openai_kwargs() configures with
# extra_body={"thinking": {"type": "disabled"}} (see ai/runtime.py). These
# providers enable thinking by default, and thinking mode rejects the forced
# function_calling tool_choice that with_structured_output() sends.
THINKING_DEFAULT_HOSTS = ("volces.com", "deepseek.com")

THINKING_MODE_ADVICE = (
    "The provider defaults to thinking mode, which rejects the "
    "function_calling tool_choice.\n"
    "    Fix: ai/runtime.py must disable thinking for this provider's "
    "hostname (see the extra_body branch).\n"
    "    Note: switching MODEL_NAME alone may not help if the provider "
    "enables thinking by default."
)

# Map the most common provider failures to actionable advice, so the
# workflow log tells the operator exactly which setting to fix.
#
# The thinking-mode / tool_choice conflict is deliberately NOT in this table:
# its advice depends on the host, so explain() resolves it first (see
# THINKING_CONFLICT_NEEDLES). Needles here are lowercased substrings of the
# message the provider actually returns, matched against str(error).lower().
DIAGNOSTICS = (
    ("authentication fail", (
        "The API key was rejected.\n"
        "    Fix: check the OPENAI_API_KEY secret belongs to the same "
        "provider as OPENAI_BASE_URL."
    )),
    ("invalid api key", (
        "The API key was rejected.\n"
        "    Fix: check the OPENAI_API_KEY secret belongs to the same "
        "provider as OPENAI_BASE_URL."
    )),
    # OpenAI's own wording for the same failure ("Incorrect API key provided"),
    # and the generic 401 shape.
    ("incorrect api key", (
        "The API key was rejected.\n"
        "    Fix: check the OPENAI_API_KEY secret belongs to the same "
        "provider as OPENAI_BASE_URL."
    )),
    ("401", (
        "The request was not authorised.\n"
        "    Fix: check the OPENAI_API_KEY secret belongs to the same "
        "provider as OPENAI_BASE_URL."
    )),
    ("insufficient balance", (
        "The account has no remaining credit.\n"
        "    Fix: top up the provider account."
    )),
    ("model not exist", (
        "The provider does not recognise MODEL_NAME.\n"
        "    Fix: check MODEL_NAME against the provider's current docs "
        "(DeepSeek renamed deepseek-chat to deepseek-flash)."
    )),
    ("model not found", (
        "The provider does not recognise MODEL_NAME.\n"
        "    Fix: check MODEL_NAME against the provider's current docs "
        "(DeepSeek renamed deepseek-chat to deepseek-flash)."
    )),
    ("does not exist", (
        "The provider does not recognise the model or endpoint.\n"
        "    Fix: check MODEL_NAME and OPENAI_BASE_URL against the "
        "provider's current docs."
    )),
    ("context length", (
        "The request exceeded the model's context window.\n"
        "    Fix: shorten the abstract, or lower MAX_TOKENS so the "
        "response cannot be truncated."
    )),
    ("connection error", (
        "The endpoint could not be reached.\n"
        "    Fix: check OPENAI_BASE_URL is correct and reachable from CI."
    )),
    ("rate limit", (
        "The provider rate-limited the probe.\n"
        "    Note: this is usually transient; re-run the workflow."
    )),
)

# Generic HTTP 400: on a thinking-default provider this is the shape a
# thinking/tool_choice rejection takes, so name that cause rather than sending
# the operator after the model name. On any other provider it stays neutral --
# telling an OpenAI user to disable thinking would be a wrong fix.
GENERIC_400_THINKING_ADVICE = (
    "The provider rejected the request as malformed (HTTP 400).\n"
    "    Most likely cause: this provider enables thinking mode by default "
    "and thinking mode rejects the function_calling tool_choice.\n"
    "    Fix: confirm ai/runtime.py disables thinking for this hostname "
    "(see the extra_body branch), then re-check MODEL_NAME."
)

GENERIC_400_ADVICE = (
    "The provider rejected the request as malformed (HTTP 400).\n"
    "    Fix: check MODEL_NAME and the request parameters against the "
    "provider's current docs."
)

UNRECOGNISED_THINKING_HOST_ADVICE = (
    "Unrecognised error from a thinking-mode provider.\n"
    "    Most likely cause: thinking mode rejects the function_calling "
    "tool_choice.\n"
    "    Fix: confirm ai/runtime.py disables thinking for this hostname, "
    "then check MODEL_NAME and OPENAI_API_KEY."
)

UNRECOGNISED_ADVICE = (
    "Unrecognised provider error. Check MODEL_NAME, OPENAI_BASE_URL and "
    "OPENAI_API_KEY against the provider's current documentation."
)

# Needles that identify the thinking-mode / forced-tool_choice conflict.
# These are the phrasings DeepSeek and Volcengine actually emit; the old
# "thinking mode does not support" sentence is not one of them.
THINKING_CONFLICT_NEEDLES = (
    "support this tool_choice",
    "tool_choice",
    "thinking mode",
    "thinking is enabled",
    "reasoning_content",
)


def _is_thinking_default_host(base_url: str | None) -> bool:
    """True when ai/runtime.py disables thinking for this host.

    Mirrors the extra_body branch in build_chat_openai_kwargs, so the advice
    and the configuration cannot drift apart. Falls back to OPENAI_BASE_URL
    when no explicit URL is passed, matching how main() configures the client.
    """
    if base_url is None:
        base_url = os.environ.get("OPENAI_BASE_URL", "")
    host = urlparse(base_url or "").hostname or ""
    return host.endswith(THINKING_DEFAULT_HOSTS)


def _looks_like_thinking_provider(error_text: str, model_name: str) -> bool:
    """Infer a thinking-default provider from the error / MODEL_NAME.

    Used only when the hostname is unknown (e.g. a caller that passes neither
    base_url nor OPENAI_BASE_URL). A message that itself names thinking mode is
    conclusive -- only a thinking-default provider phrases its rejection that
    way -- as are DeepSeek's model ids.
    """
    haystack = f"{error_text} {model_name}".lower()
    return any(
        token in haystack
        for token in (
            "deepseek",
            "volces",
            "doubao",
            "ark.cn-beijing",
            "thinking mode",
            "thinking is enabled",
        )
    )


def explain(error: Exception, base_url: str | None = None) -> str:
    """Turn a provider exception into an actionable hint.

    ``base_url`` selects provider-specific advice. The thinking-mode fix is
    only offered for hosts that actually default to thinking mode; for every
    other provider the neutral message is returned instead of a wrong fix.
    """
    text = str(error).lower()
    thinking_host = _is_thinking_default_host(base_url)
    if not thinking_host:
        # No usable hostname: fall back to clues in the message / MODEL_NAME so
        # the DeepSeek outage is still diagnosed rather than reported as
        # "Unrecognised provider error".
        thinking_host = _looks_like_thinking_provider(
            text, os.environ.get("MODEL_NAME", "")
        )

    # Most-specific first: a precise tool_choice/thinking conflict outranks the
    # generic HTTP-400 entry below.
    if any(needle in text for needle in THINKING_CONFLICT_NEEDLES):
        if thinking_host:
            return THINKING_MODE_ADVICE
        # The provider rejected tool_choice, but it is not one configured to
        # disable thinking -- do not tell the operator to edit runtime.py.
        return (
            "The provider rejected the tool_choice used for structured "
            "output.\n"
            "    Fix: check that MODEL_NAME supports function calling / "
            "structured output, and compare against the provider's docs."
        )

    for needle, advice in DIAGNOSTICS:
        if needle in text:
            return advice

    # Generic HTTP 400 from a thinking-default host: name the likely cause
    # rather than sending the operator after the model name.
    if "400" in text:
        return GENERIC_400_THINKING_ADVICE if thinking_host else GENERIC_400_ADVICE

    if thinking_host:
        return UNRECOGNISED_THINKING_HOST_ADVICE

    return UNRECOGNISED_ADVICE


def main() -> int:
    model_name = os.environ.get("MODEL_NAME", "").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    api_key = os.environ.get("OPENAI_API_KEY", "")

    print("=" * 60)
    print("LLM pre-flight check")
    print(f"  MODEL_NAME      : {model_name or '(not set)'}")
    print(f"  OPENAI_BASE_URL : {base_url or '(not set)'}")
    print(f"  OPENAI_API_KEY  : {'set' if api_key.strip() else '(not set)'}")
    print("=" * 60)

    # build_chat_openai_kwargs raises ValueError on missing configuration,
    # reusing the same validation the real job performs.
    try:
        kwargs = build_chat_openai_kwargs(
            model_name=model_name, base_url=base_url, api_key=api_key
        )
    except ValueError as error:
        print(f"\n[FAIL] {error}", file=sys.stderr)
        print(
            "\n    Fix: set these as repository Secrets/Variables "
            "(Settings -> Secrets and variables -> Actions).",
            file=sys.stderr,
        )
        return 1

    if kwargs.get("extra_body"):
        print(f"  provider options: {kwargs['extra_body']}")

    # Identical construction to enhance.py: structured output via
    # function_calling is what triggers the tool_choice / thinking conflict.
    llm = ChatOpenAI(**kwargs).with_structured_output(
        Structure, method="function_calling"
    )

    try:
        result = llm.invoke(
            [
                ("system", PROBE_SYSTEM),
                ("user", PROBE_USER),
            ]
        )
    except Exception as error:  # noqa: BLE001 - surface any provider failure
        print(f"\n[FAIL] The model is not usable: {error}", file=sys.stderr)
        print(f"\n    {explain(error, base_url)}", file=sys.stderr)
        print(
            "\nAborting before the crawl so no time is wasted.",
            file=sys.stderr,
        )
        return 1

    missing = [f for f in Structure.model_fields if f not in result.model_dump()]
    if missing:
        print(
            f"\n[FAIL] Model responded but omitted required fields: {missing}",
            file=sys.stderr,
        )
        print(
            "    Fix: the model must support function calling / structured output.",
            file=sys.stderr,
        )
        return 1

    print("\n[OK] Model responded to a structured request successfully.")
    print("     The enhancement step is expected to work.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
