"""Context manager for inter-agent communication."""

import copy
from typing import Any, Dict, Literal, Optional
from ..config.schema import OrchestrationDefinition


class ContextManager:
    """Manages inter-agent communication context."""

    def __init__(self, communication_mode: Literal["shared_context", "message_passing", "both"]):
        self.communication_mode = communication_mode
        self.shared_context: Dict[str, Any] = {}
        self.messages: Dict[str, Any] = {}  # output_as -> value
        self.initial_input: Dict[str, Any] = {}  # orchestration input (always available)
        # Keys written since the last branch() call (used by merge_from to avoid
        # clobbering sibling writes with stale pre-fork copies).
        self._dirty_shared: set = set()
        self._dirty_messages: set = set()

    def format_input(self, template: Optional[str], **kwargs: Any) -> Optional[str]:
        """Format an input template with available context.

        Args:
            template: Input template string (may contain {variable} placeholders)
            **kwargs: Additional variables for formatting

        Returns:
            Formatted string or None if template is None
        """
        if template is None:
            return None

        # Merge initial input (orchestration input, always available), shared context, messages, and kwargs
        context = {}
        context.update(self.initial_input)  # orchestration input always available
        if self.communication_mode in ("shared_context", "both"):
            context.update(self.shared_context)
        if self.communication_mode in ("message_passing", "both"):
            context.update(self.messages)
        context.update(kwargs)

        class _DefaultStr(dict):
            def __missing__(self, key: str) -> str:
                return ""

        try:
            return template.format_map(_DefaultStr(context))
        except Exception:
            return template

    def set_output(self, name: str, value: Any) -> None:
        """Set an output value (for message passing).

        Args:
            name: Output name
            value: Output value
        """
        if self.communication_mode in ("message_passing", "both"):
            self.messages[name] = value
            self._dirty_messages.add(name)

    def get_output(self, name: str) -> Any:
        """Get an output value.

        Args:
            name: Output name

        Returns:
            Output value or None if not found
        """
        return self.messages.get(name)

    def set_shared(self, key: str, value: Any) -> None:
        """Set a shared context value.

        Args:
            key: Context key
            value: Context value
        """
        if self.communication_mode in ("shared_context", "both"):
            self.shared_context[key] = value
            self._dirty_shared.add(key)

    def get_shared(self, key: str, default: Any = None) -> Any:
        """Get a shared context value.

        Args:
            key: Context key
            default: Default value if key not found

        Returns:
            Context value or default
        """
        return self.shared_context.get(key, default)

    def update_shared(self, updates: Dict[str, Any]) -> None:
        """Update shared context with multiple values.

        Args:
            updates: Dictionary of key-value pairs to update
        """
        if self.communication_mode in ("shared_context", "both"):
            self.shared_context.update(updates)
            self._dirty_shared.update(updates.keys())

    def set_initial_input(self, data: Dict[str, Any]) -> None:
        """Set initial orchestration input (always available for templating).

        Args:
            data: Dictionary of initial input values
        """
        self.initial_input = dict(data) if data else {}

    def branch(self) -> "ContextManager":
        """Create an isolated snapshot of this context for a parallel branch.

        The branch starts with copies of all current state so that reads
        within the branch see the pre-fork values. Writes stay local to the
        branch until merged back via merge_from().
        """
        snapshot = ContextManager(self.communication_mode)
        snapshot.initial_input = copy.deepcopy(self.initial_input)
        snapshot.shared_context = copy.deepcopy(self.shared_context)
        snapshot.messages = copy.deepcopy(self.messages)
        # Dirty sets start empty: only keys written by this branch are merged back.
        snapshot._dirty_shared = set()
        snapshot._dirty_messages = set()
        return snapshot

    def merge_from(self, branch: "ContextManager") -> None:
        """Merge writes from a completed parallel branch back into this context.

        Only keys the branch actually wrote (tracked via _dirty_shared and
        _dirty_messages) are merged back. This prevents a branch from clobbering
        writes made by a previously-merged sibling branch with stale pre-fork values.
        Branches that write to the same key will have their result determined by
        merge iteration order (last-write-wins).
        """
        for key in branch._dirty_shared:
            self.shared_context[key] = branch.shared_context[key]
        for key in branch._dirty_messages:
            self.messages[key] = branch.messages[key]

    def to_dict(self) -> Dict[str, Any]:
        """Export context as dictionary."""
        result = {}
        result["initial_input"] = self.initial_input.copy()
        if self.communication_mode in ("shared_context", "both"):
            result["shared_context"] = self.shared_context.copy()
        if self.communication_mode in ("message_passing", "both"):
            result["messages"] = self.messages.copy()
        return result
