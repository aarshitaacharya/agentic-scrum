"""
llm.py — one place that builds the chat model.

Gemini 2.5 Flash on the free tier, reached through LangChain's chat-model
interface rather than the raw SDK. The reason for the indirection is the ReAct
loop in agents/react.py: it only ever calls `.invoke(messages)` and reads
`.content`, so it works against any LangChain chat model. Swapping in Bedrock
(`ChatBedrockConverse`) or a local Ollama model is a change to this file alone.
"""

from __future__ import annotations

from scrum.config import SETTINGS, Settings, resolve_gemini_api_key

_LLM = None


class MissingAPIKey(RuntimeError):
    pass


def build_llm(settings: Settings = SETTINGS, temperature: float | None = None):
    """Construct a LangChain chat model. Cached, since agents share one."""
    global _LLM
    if _LLM is not None and temperature is None:
        return _LLM

    api_key = resolve_gemini_api_key(settings)
    if not api_key:
        raise MissingAPIKey(
            "No Gemini API key available.\n"
            "  Locally: get a free key at https://aistudio.google.com/app/apikey "
            "and put it in .env as GEMINI_API_KEY=...\n"
            "  On AWS:  GEMINI_API_KEY_SECRET_ARN must point at a readable secret, "
            "and the function's role needs secretsmanager:GetSecretValue."
        )

    from langchain_google_genai import ChatGoogleGenerativeAI

    llm = ChatGoogleGenerativeAI(
        model=settings.model_name,
        google_api_key=api_key,
        # Low but non-zero. At 0.0 a stuck agent repeats the identical failing
        # action on every retry; a little noise lets attempt 2 differ from
        # attempt 1, which is the entire point of having a retry.
        temperature=settings.temperature if temperature is None else temperature,
        max_retries=2,
    )

    if temperature is None:
        _LLM = llm
    return llm
