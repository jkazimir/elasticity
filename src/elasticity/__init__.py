"""Elasticity: Configuration-driven agent orchestration framework."""

import asyncio
from typing import Any, Dict, Optional

from .orchestration import Orchestration
from .conductor import Conductor

__version__ = "0.1.0"


def run(config_path: str, orchestration_name: str, input_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Convenience function to run an orchestration."""
    orch = Orchestration.from_file(config_path)
    return orch.run_sync(orchestration_name, input_data=input_data)
