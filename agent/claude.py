"""Claude request options shared by the chat, the phone line and the call summaries."""

# If a safety classifier wrongly declines a request, another model answers it. Haiku has
# no server-side fallback.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = ("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5")


def refusal_fallback(model: str) -> dict:
    if model.startswith(FALLBACK_MODELS):
        return {"betas": [FALLBACK_BETA], "fallbacks": "default"}
    return {}
