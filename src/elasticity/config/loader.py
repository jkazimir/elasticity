"""YAML configuration loader."""

from pathlib import Path
from typing import Union
import yaml

from .schema import Config
from ..errors import ConfigError


def load_config(path: Union[str, Path]) -> Config:
    """Load and parse a YAML configuration file.

    Args:
        path: Path to YAML file

    Returns:
        Validated Config object

    Raises:
        ConfigError: If file cannot be read or parsed
    """
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Configuration file not found: {path}")

    try:
        with open(path, "r") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from e
    except Exception as e:
        raise ConfigError(f"Error reading {path}: {e}") from e

    if data is None:
        raise ConfigError(f"Empty configuration file: {path}")

    # Normalize None values and empty lists to empty dicts for optional sections
    if isinstance(data, dict):
        for key in ["agent_types", "tools", "orchestrations", "mcp_servers"]:
            if key not in data or data[key] is None or data[key] == []:
                data[key] = {}

    try:
        return Config.model_validate(data)
    except Exception as e:
        raise ConfigError(f"Invalid configuration schema in {path}: {e}") from e
