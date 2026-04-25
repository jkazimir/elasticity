"""Session management for conversational orchestrations."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any, Dict, List


@dataclass
class Session:
    """Session for conversational orchestration execution.
    
    Maintains context and message history across multiple turns.
    """
    
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    context: Dict[str, Any] = field(default_factory=dict)
    message_history: List[Dict[str, str]] = field(default_factory=list)
    max_history_turns: int = 20
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    
    def add_turn(self, user_message: str, assistant_response: str) -> None:
        """Add a conversation turn to the history.
        
        Args:
            user_message: User's message
            assistant_response: Assistant's response
        """
        self.message_history.append({"role": "user", "content": user_message})
        self.message_history.append({"role": "assistant", "content": assistant_response})
        
        # Trim history if it exceeds max_history_turns
        if len(self.message_history) > self.max_history_turns * 2:
            # Keep the most recent turns (each turn is 2 messages: user + assistant)
            self.message_history = self.message_history[-(self.max_history_turns * 2):]
    
    def get_history(self) -> List[Dict[str, str]]:
        """Get the conversation history, windowed by max_history_turns.
        
        Returns:
            List of message dicts with 'role' and 'content' keys
        """
        # Return the most recent messages (already trimmed in add_turn)
        return self.message_history.copy()
    
    def clear(self) -> None:
        """Clear the session history and context."""
        self.message_history.clear()
        self.context.clear()
