"""The model behind the dataset agent: any OpenAI-compatible chat completions endpoint.

The endpoint, model and token come from settings (LLM_BASE_URL, LLM_MODEL, LLM_API_KEY). Swapping
providers means changing those, not this code.
"""

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.settings import Settings


class AgentError(Exception):
    """A model, prompt or tool setup step failed. The message is safe to show."""


def chat_model(settings: Settings) -> BaseChatModel | None:
    """The chat model for the dataset agent, or None when no endpoint is configured."""
    if not settings.agent_enabled:
        return None
    return ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        # Servers without auth (a local vLLM, say) still need a non-empty key for the client
        api_key=settings.llm_api_key.get_secret_value().strip() if settings.llm_api_key else "EMPTY",
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout_seconds,
        max_retries=1,
    )
