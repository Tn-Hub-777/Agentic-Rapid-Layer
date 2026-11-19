"""LLM provider abstraction. Choose with LLM_PROVIDER / LLM_MODEL env vars; no code changes needed.

mock   -> scripted offline model (default)
ollama -> local model via Ollama (free)
groq   -> Groq free tier
gemini -> Google AI Studio free tier
"""
from __future__ import annotations

import os

from langchain.agents.middleware import ModelFallbackMiddleware
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from app.config import settings
from app.llm.mock import MockChatModel

_PREFIX = {"ollama": "ollama", "groq": "groq", "gemini": "google_genai", "openai": "openai"}


def _build(provider: str, model: str) -> BaseChatModel:
    if provider == "mock":
        return MockChatModel()
    if provider not in _PREFIX:
        raise ValueError(f"Unknown LLM provider '{provider}'. Use mock, ollama, groq or gemini.")
    if provider == "gemini" and not os.getenv("GOOGLE_API_KEY"):
        raise ValueError(
            "Gemini requires GOOGLE_API_KEY. Add it to .env (copy .env.example) "
            "or set it in your environment."
        )
    kwargs = {"temperature": 0}
    return init_chat_model(f"{_PREFIX[provider]}:{model}", **kwargs)


def get_chat_model() -> BaseChatModel:
    return _build(settings.llm_provider, settings.llm_model)


def get_fallback_middleware() -> list:
    """If FALLBACK_PROVIDER is set, agents automatically retry on that model when the primary errors."""
    if settings.fallback_provider and settings.fallback_provider != "mock":
        return [ModelFallbackMiddleware(_build(settings.fallback_provider, settings.fallback_model))]
    return []
