"""Pre-flight check for the LLM endpoint used by the enhancement job.

Runs before the (slow, ~1h) crawl + enhancement pipeline so that a
misconfigured provider fails in seconds instead of after a full crawl.

It deliberately reuses the exact same construction path as enhance.py --
build_chat_openai_kwargs(...) + ChatOpenAI(...).with_structured_output(...,
method="function_calling") -- because the failure this guards against
(HTTP 400 "Thinking mode does not support this tool_choice") only surfaces
when structured output is actually requested. A plain connectivity ping
cannot detect it.

Exit codes:
    0  the model answered a structured request successfully
    1  the model is not usable; a human-readable reason was printed
"""

import os
import sys

from langchain_openai import ChatOpenAI

from runtime import build_chat_openai_kwargs
from structure import Structure

# Minimal prompt: enough to exercise function_calling, small enough to be
# effectively free (a few tokens).
PROBE_SYSTEM = "You are a connectivity probe. Reply using the requested schema."
PROBE_USER = "Summarize this into the schema: The probe checks API connectivity."

# Map the most common provider failures to actionable advice, so the
# workflow log tells the operator exactly which setting to fix.
DIAGNOSTICS = (
    ("thinking mode does not support", (
        "The provider defaults to thinking mode, which rejects the "
        "function_calling tool_choice.\n"
        "    Fix: ai/runtime.py must disable thinking for this provider's "
        "hostname (see the extra_body branch).\n"
        "    Note: switching MODEL_NAME alone may not help if the provider "
        "enables thinking by default."
    )),
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
    ("insufficient balance", (
        "The account has no remaining credit.\n"
        "    Fix: top up the provider account."
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
    ("connection error", (
        "The endpoint could not be reached.\n"
        "    Fix: check OPENAI_BASE_URL is correct and reachable from CI."
    )),
    ("rate limit", (
        "The provider rate-limited the probe.\n"
        "    Note: this is usually transient; re-run the workflow."
    )),
)


def explain(error: Exception) -> str:
    """Turn a provider exception into an actionable hint."""
    text = str(error).lower()
    for needle, advice in DIAGNOSTICS:
        if needle in text:
            return advice
    return (
        "Unrecognised provider error. Check MODEL_NAME, OPENAI_BASE_URL and "
        "OPENAI_API_KEY against the provider's current documentation."
    )


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
        print(f"\n    {explain(error)}", file=sys.stderr)
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
