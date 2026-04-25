"""Backend registry and resolution."""

from typing import Tuple

from .base import Backend
from .openai import OpenAIBackend
from .anthropic import AnthropicBackend
from ..errors import ConfigError, BackendError

# Cache backend instances per provider
_backend_cache: dict[str, Backend] = {}


def resolve_backend(model_string: str) -> Tuple[Backend, str]:
    """Resolve backend and model name from model string.

    Model string format: "provider/model-name"
    Examples:
        - "openai/gpt-4o" -> (OpenAIBackend, "gpt-4o")
        - "anthropic/claude-sonnet-4-6" -> (AnthropicBackend, "claude-sonnet-4-6")

    Args:
        model_string: Model identifier with provider prefix

    Returns:
        Tuple of (Backend instance, model_name without prefix)

    Raises:
        ConfigError: If model string format is invalid
        BackendError: If backend SDK is not installed
    """
    if "/" not in model_string:
        raise ConfigError(
            f"Invalid model format: '{model_string}'. "
            "Expected format: 'provider/model-name' (e.g., 'openai/gpt-4o', 'anthropic/claude-sonnet-4-6')"
        )

    provider, model_name = model_string.split("/", 1)

    if not provider or not model_name:
        raise ConfigError(
            f"Invalid model format: '{model_string}'. "
            "Provider and model name cannot be empty."
        )

    # Get or create backend instance
    if provider not in _backend_cache:
        try:
            if provider == "openai":
                _backend_cache[provider] = OpenAIBackend()
            elif provider == "anthropic":
                _backend_cache[provider] = AnthropicBackend()
            else:
                raise ConfigError(
                    f"Unknown backend provider: '{provider}'. "
                    f"Supported providers: openai, anthropic"
                )
        except BackendError as e:
            # Re-raise backend errors (e.g., SDK not installed)
            raise
        except Exception as e:
            raise BackendError(f"Failed to initialize {provider} backend: {e}") from e

    return _backend_cache[provider], model_name
