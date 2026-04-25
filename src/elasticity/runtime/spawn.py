"""Dynamic agent spawning manager."""

from typing import Dict, List, Optional
from ..config.schema import AgentTypeDefinition
from ..errors import SpawnError


class SpawnManager:
    """Manages dynamic agent spawning."""

    def __init__(self):
        self._active_spawns: Dict[str, List[str]] = {}  # parent_agent -> [child_ids]

    def can_spawn(
        self, parent_agent_name: str, agent_type: AgentTypeDefinition, child_type_name: str
    ) -> bool:
        """Check if an agent can spawn a child of the given type.

        Args:
            parent_agent_name: Name of the parent agent
            agent_type: Parent agent type definition
            child_type_name: Name of child agent type to spawn

        Returns:
            True if spawning is allowed
        """
        if child_type_name not in agent_type.can_spawn:
            return False

        # Check max concurrent spawns
        if agent_type.max_concurrent_spawns is not None:
            active_count = len(self._active_spawns.get(parent_agent_name, []))
            if active_count >= agent_type.max_concurrent_spawns:
                return False

        return True

    def register_spawn(self, parent_agent_name: str, child_id: str) -> None:
        """Register a spawn event.

        Args:
            parent_agent_name: Name of the parent agent
            child_id: Unique ID for the spawned child
        """
        if parent_agent_name not in self._active_spawns:
            self._active_spawns[parent_agent_name] = []
        self._active_spawns[parent_agent_name].append(child_id)

    def unregister_spawn(self, parent_agent_name: str, child_id: str) -> None:
        """Unregister a spawn event (when child completes).

        Args:
            parent_agent_name: Name of the parent agent
            child_id: Unique ID for the spawned child
        """
        if parent_agent_name in self._active_spawns:
            if child_id in self._active_spawns[parent_agent_name]:
                self._active_spawns[parent_agent_name].remove(child_id)

    def get_active_spawns(self, parent_agent_name: str) -> List[str]:
        """Get list of active child IDs for a parent agent.

        Args:
            parent_agent_name: Name of the parent agent

        Returns:
            List of active child IDs
        """
        return self._active_spawns.get(parent_agent_name, []).copy()
