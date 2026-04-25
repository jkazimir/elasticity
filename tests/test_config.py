"""Tests for configuration loading and validation."""

import pytest
from pathlib import Path
from elasticity.config.loader import load_config
from elasticity.config.schema import InputHandlingConfig
from elasticity.config.validator import validate_references
from elasticity.errors import ConfigError, ValidationError, ConfigReferenceError


def test_load_valid_config():
    """Test loading a valid configuration."""
    config_path = Path(__file__).parent.parent / "examples" / "research_and_write.yaml"
    config = load_config(config_path)

    assert len(config.agent_types) > 0
    assert len(config.orchestrations) > 0
    assert "researcher" in config.agent_types
    assert "research_and_write" in config.orchestrations


def test_load_invalid_yaml(tmp_path):
    """Test loading invalid YAML."""
    invalid_yaml = tmp_path / "invalid.yaml"
    invalid_yaml.write_text("invalid: yaml: content: [unclosed")

    with pytest.raises(ConfigError):
        load_config(invalid_yaml)


def test_load_missing_file():
    """Test loading a non-existent file."""
    with pytest.raises(ConfigError):
        load_config("nonexistent.yaml")


def test_validate_references():
    """Test reference validation."""
    config_path = Path(__file__).parent.parent / "examples" / "research_and_write.yaml"
    config = load_config(config_path)
    validate_references(config)  # Should not raise


def test_validate_missing_agent_reference(tmp_path):
    """Test validation fails when agent type is missing."""
    invalid_config = tmp_path / "invalid.yaml"
    invalid_config.write_text(
        """
agent_types:
  researcher:
    model: gpt-4o
    system_prompt: "Test"

orchestrations:
  test:
    flow:
      - agent: nonexistent_agent
        input: "test"
"""
    )

    config = load_config(invalid_config)
    with pytest.raises(ConfigReferenceError):
        validate_references(config)


def test_load_conversational_assistant_with_builtin_tools():
    """Test that conversational_assistant.yaml loads successfully with builtin-only tools."""
    config_path = Path(__file__).parent.parent / "examples" / "conversational_assistant.yaml"
    config = load_config(config_path)
    validate_references(config)  # Should not raise
    
    # Verify builtin tools are loaded
    assert "web_search" in config.tools
    assert "file_read" in config.tools
    assert "file_write" in config.tools
    assert "file_list" in config.tools
    
    # Verify builtin tools have builtin set
    assert config.tools["file_read"].builtin == "file_read"
    assert config.tools["file_write"].builtin == "file_write"


def test_input_handling_config_defaults():
    """InputHandlingConfig has sensible defaults."""
    config = InputHandlingConfig(mode="queue")
    assert config.mode == "queue"
    assert config.queue_limit == 10


def test_input_handling_config_interrupt_graceful_requires_delivery():
    """Graceful interrupt requires at least one delivery mechanism."""
    with pytest.raises(ValueError, match="interrupt_delivery"):
        InputHandlingConfig(
            mode="interrupt",
            interrupt_behavior="graceful",
            interrupt_delivery=[],
        )


def test_input_handling_config_interrupt_graceful_with_delivery():
    """Graceful interrupt with delivery mechanisms is valid."""
    config = InputHandlingConfig(
        mode="interrupt",
        interrupt_behavior="graceful",
        interrupt_delivery=["event", "context"],
    )
    assert config.interrupt_behavior == "graceful"
    assert config.interrupt_delivery == ["event", "context"]


def test_step_input_accepts_spawn_context():
    """StepInput schema accepts spawn_context without warnings."""
    from elasticity.config.schema import StepInput
    step = StepInput(
        agent="coordinator",
        input="do work",
        spawn_strategy="dynamic",
        spawn_context="ARCHITECTURE:\n{arch_plan}",
    )
    assert step.spawn_context == "ARCHITECTURE:\n{arch_plan}"


def test_step_input_spawn_context_defaults_none():
    """spawn_context defaults to None when not provided."""
    from elasticity.config.schema import StepInput
    step = StepInput(agent="worker", input="task")
    assert step.spawn_context is None


def test_validate_spawn_context_without_spawn_strategy(tmp_path):
    """Validator raises ConfigReferenceError when spawn_context is used without spawn_strategy."""
    config_yaml = tmp_path / "bad.yaml"
    config_yaml.write_text(
        """
agent_types:
  worker:
    model: openai/gpt-4o
    system_prompt: "Test"

orchestrations:
  test:
    flow:
      - agent: worker
        input: "task"
        spawn_context: "shared context"
"""
    )
    config = load_config(config_yaml)
    with pytest.raises(ConfigReferenceError, match="spawn_context"):
        validate_references(config)


def test_validate_spawn_context_with_spawn_strategy_valid(tmp_path):
    """spawn_context with spawn_strategy: dynamic passes validation."""
    config_yaml = tmp_path / "good.yaml"
    config_yaml.write_text(
        """
agent_types:
  coordinator:
    model: openai/gpt-4o
    system_prompt: "You are the coordinator."
    can_spawn:
      - worker
  worker:
    model: openai/gpt-4o
    system_prompt: "You are the worker."

orchestrations:
  test:
    flow:
      - agent: coordinator
        input: "do work"
        spawn_strategy: dynamic
        spawn_context: "SHARED CONTEXT"
        collect_as: results
"""
    )
    config = load_config(config_yaml)
    validate_references(config)  # Should not raise
