"""Reference resolution for configuration.

Intentionally left as a no-op pass-through. Future uses could include:
- Expanding macro references (e.g. agent type inheritance)
- Inlining shared fragments from `include` files after they are merged
- Normalising deprecated fields before graph compilation
"""

from ..config.schema import Config


def resolve_references(config: Config) -> Config:
    """Return the config unchanged. Reserved for future transformation passes."""
    return config
