"""Public client waiting budgets, derived from model timeout/retry configuration.

These are browser deadlines, not server cancellation guarantees: SDK timeouts are
per network phase, and SQL/vector operations also have their own timeouts.
Allow up to 60s Retry-After per SDK retry plus 30s for local processing.
Never serialize Settings itself: it contains credentials.
"""

from .config import Settings


def request_timeouts_ms(settings: Settings) -> dict[str, int]:
    retries = settings.bailian_max_retries

    def model_call(seconds: int) -> int:
        return seconds * (retries + 1) + 60 * retries

    embedding = model_call(settings.bailian_embedding_timeout_seconds)
    llm = model_call(settings.bailian_llm_timeout_seconds)
    # setTimeout uses signed 32-bit milliseconds in Chromium.
    def budget(seconds: int) -> int:
        return min(2_147_483_647, max(30_000, seconds * 1000))

    return {
        "search": budget(embedding + 30),
        "ask": budget(embedding + llm + 30),
        "api_key": budget(embedding + 30),
        "model_config": budget(embedding + llm + 30),
    }
