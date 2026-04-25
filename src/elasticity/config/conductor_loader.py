"""Loader for conductor configuration files."""

from pathlib import Path

import yaml

from .conductor_schema import ConductorConfig


def load_conductor_config(config_path: Path) -> ConductorConfig:
    """Load and validate a conductor config from a YAML file.

    Args:
        config_path: Absolute path to the conductor YAML file.

    Returns:
        Validated ConductorConfig.

    Raises:
        ValueError: If the file is not a valid mapping or lacks a 'conductor' section.
        pydantic.ValidationError: If the config fails schema validation.
    """
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Conductor config must be a YAML mapping: {config_path}")

    if "conductor" not in raw:
        raise ValueError(
            f"Conductor config must have a 'conductor' section: {config_path}"
        )

    return ConductorConfig.model_validate(raw)
