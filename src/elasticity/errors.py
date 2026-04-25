"""Custom exceptions for Elasticity."""


class ElasticityError(Exception):
    """Base exception for all Elasticity errors."""


class ConfigError(ElasticityError):
    """Error in configuration file."""


class ValidationError(ElasticityError):
    """Configuration validation error."""


class ConfigReferenceError(ElasticityError):
    """Reference to undefined agent type, tool, or orchestration."""


class ExecutionError(ElasticityError):
    """Error during orchestration execution."""


class AgentError(ElasticityError):
    """Error in agent execution."""


class ToolError(ElasticityError):
    """Error in tool execution."""


class SpawnError(ElasticityError):
    """Error in agent spawning."""


class BackendError(ElasticityError):
    """Error in backend execution or configuration."""


class InputHandlingError(ElasticityError):
    """Base for input handling errors."""


class OrchestrationInterrupted(InputHandlingError):
    """Raised when a cancel interrupt stops orchestration execution."""

    def __init__(self, message: str, node: str = ""):
        super().__init__(f"Orchestration interrupted at {node}: {message}")
        self.interrupt_message = message
        self.interrupted_node = node


class QueueOverflow(InputHandlingError):
    """Raised when the input queue is full and overflow policy rejects new input."""
