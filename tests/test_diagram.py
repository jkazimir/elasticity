"""Tests for sequence diagram generation."""

import pytest
from pathlib import Path

from elasticity.config.loader import load_config
from elasticity.diagram import generate_sequence_diagram
from elasticity.errors import ConfigError


def test_generate_diagram_simple_flow():
    """Test generating a diagram for a simple sequential flow."""
    config_path = Path(__file__).parent.parent / "examples" / "ralph_loop.yaml"
    config = load_config(config_path)

    diagram = generate_sequence_diagram(config, "ralph_loop")

    # Check basic structure
    assert "sequenceDiagram" in diagram
    assert "participant planner" in diagram
    assert "participant worker" in diagram
    assert "participant evaluator" in diagram
    assert "participant reviewer" in diagram

    # Check flow elements
    assert "loop" in diagram
    assert "planner->>worker" in diagram
    assert "worker->>evaluator" in diagram
    assert "worker->>reviewer" in diagram


def test_generate_diagram_parallel_flow():
    """Test generating a diagram for a parallel flow."""
    # Use cognitive_architecture.yaml which has parallel blocks and valid tools structure
    config_path = Path(__file__).parent.parent / "examples" / "cognitive_architecture.yaml"
    config = load_config(config_path)

    diagram = generate_sequence_diagram(config, "cognitive_cycle")

    # Check participants (should include agents from parallel blocks)
    assert "participant wernicke" in diagram
    assert "participant amygdala" in diagram
    assert "participant hippocampus" in diagram

    # Check parallel block
    assert "par" in diagram
    assert "and" in diagram
    assert "end" in diagram


def test_generate_diagram_route():
    """Test generating a diagram with route/conditional branching."""
    config_path = Path(__file__).parent.parent / "examples" / "cognitive_architecture.yaml"
    config = load_config(config_path)

    diagram = generate_sequence_diagram(config, "cognitive_cycle")

    # Check route block
    assert "alt" in diagram
    assert "else" in diagram

    # Check participants
    assert "participant thalamus" in diagram
    assert "participant wernicke" in diagram
    assert "participant prefrontal_cortex" in diagram


def test_generate_diagram_missing_orchestration():
    """Test that generating a diagram for a missing orchestration raises KeyError."""
    config_path = Path(__file__).parent.parent / "examples" / "ralph_loop.yaml"
    config = load_config(config_path)

    with pytest.raises(KeyError):
        generate_sequence_diagram(config, "nonexistent_orchestration")


def test_generate_diagram_supervise():
    """Test generating a diagram with supervise step."""
    config_path = Path(__file__).parent.parent / "examples" / "cognitive_architecture.yaml"
    config = load_config(config_path)

    diagram = generate_sequence_diagram(config, "cognitive_cycle")

    # Check supervise elements
    assert "supervisor" in diagram.lower() or "anterior_cingulate" in diagram
    assert "broca" in diagram


def test_generate_diagram_loop():
    """Test generating a diagram with loop."""
    config_path = Path(__file__).parent.parent / "examples" / "ralph_loop.yaml"
    config = load_config(config_path)

    diagram = generate_sequence_diagram(config, "ralph_loop")

    # Check loop structure
    assert "loop" in diagram
    assert "end" in diagram
    # Should have until condition
    assert "until" in diagram.lower() or "completion_score" in diagram


def test_generate_diagram_interval():
    """Test generating a diagram with interval step."""
    config_path = Path(__file__).parent.parent / "examples" / "cognitive_architecture.yaml"
    config = load_config(config_path)

    diagram = generate_sequence_diagram(config, "cognitive_cycle")

    # Check interval elements
    assert "interval" in diagram.lower() or "every" in diagram.lower() or "default_mode" in diagram


def test_diagram_participant_ordering():
    """Test that participants appear in order of first use."""
    config_path = Path(__file__).parent.parent / "examples" / "ralph_loop.yaml"
    config = load_config(config_path)

    diagram = generate_sequence_diagram(config, "ralph_loop")

    # Participants should be in order: planner, worker, evaluator, reviewer
    lines = diagram.split("\n")
    participant_lines = [line for line in lines if "participant" in line]
    
    assert len(participant_lines) == 4
    assert "planner" in participant_lines[0]
    assert "worker" in participant_lines[1]
    assert "evaluator" in participant_lines[2]
    assert "reviewer" in participant_lines[3]


def test_diagram_output_as_labels():
    """Test that output_as variables appear in arrows."""
    config_path = Path(__file__).parent.parent / "examples" / "ralph_loop.yaml"
    config = load_config(config_path)

    diagram = generate_sequence_diagram(config, "ralph_loop")

    # Check that output_as variables appear
    assert "acceptance_criteria" in diagram
    assert "work" in diagram
    assert "final_output" in diagram
    # Note: evaluation is produced inside the loop but may not appear in the main flow
    # depending on how the loop is rendered
