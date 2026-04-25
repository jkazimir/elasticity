"""Tests for compiler (graph building)."""

import pytest
from pathlib import Path
from elasticity.config.loader import load_config
from elasticity.compiler.graph import GraphBuilder, NodeType


def test_build_simple_graph():
    """Test building a simple execution graph."""
    config_path = Path(__file__).parent.parent / "examples" / "research_and_write.yaml"
    config = load_config(config_path)

    builder = GraphBuilder(config)
    graph = builder.build("research_and_write")

    assert graph.entry_node is not None
    assert len(graph.nodes) > 0

    # Check that we have agent nodes
    agent_nodes = [n for n in graph.nodes.values() if n.node_type == NodeType.AGENT]
    assert len(agent_nodes) >= 2  # researcher and writer


def test_build_parallel_graph():
    """Test building a graph with parallel execution."""
    config_path = Path(__file__).parent.parent / "examples" / "fan_out_research.yaml"
    config = load_config(config_path)

    builder = GraphBuilder(config)
    graph = builder.build("fan_out_research")

    assert graph.entry_node is not None

    # Check for parallel node
    parallel_nodes = [n for n in graph.nodes.values() if n.node_type == NodeType.PARALLEL]
    assert len(parallel_nodes) > 0


def test_build_loop_graph():
    """Test building a graph with loop."""
    config_path = Path(__file__).parent.parent / "examples" / "iterative_refinement.yaml"
    config = load_config(config_path)

    builder = GraphBuilder(config)
    graph = builder.build("iterative_refinement")

    assert graph.entry_node is not None

    # Check for loop node
    loop_nodes = [n for n in graph.nodes.values() if n.node_type == NodeType.LOOP]
    assert len(loop_nodes) > 0


def test_agent_step_preserves_spawn_context_in_config():
    """GraphBuilder propagates spawn_context from StepInput to GraphNode config."""
    from elasticity.config.schema import Config, AgentTypeDefinition, OrchestrationDefinition, StepInput

    config = Config(
        agent_types={
            "coordinator": AgentTypeDefinition(
                model="openai/gpt-4o",
                system_prompt="Coordinator",
                can_spawn=["worker"],
            ),
            "worker": AgentTypeDefinition(
                model="openai/gpt-4o",
                system_prompt="Worker",
            ),
        },
        tools={},
        orchestrations={
            "test": OrchestrationDefinition(
                flow=[
                    StepInput(
                        agent="coordinator",
                        input="do work",
                        spawn_strategy="dynamic",
                        spawn_context="SHARED:\n{some_var}",
                        collect_as="results",
                    )
                ]
            )
        },
    )

    builder = GraphBuilder(config)
    graph = builder.build("test")

    agent_nodes = [n for n in graph.nodes.values() if n.node_type == NodeType.AGENT]
    assert len(agent_nodes) == 1
    assert agent_nodes[0].config["spawn_context"] == "SHARED:\n{some_var}"


def test_agent_step_spawn_context_none_when_absent():
    """GraphNode config has spawn_context=None when not set in StepInput."""
    from elasticity.config.schema import Config, AgentTypeDefinition, OrchestrationDefinition, StepInput

    config = Config(
        agent_types={
            "worker": AgentTypeDefinition(
                model="openai/gpt-4o",
                system_prompt="Worker",
            ),
        },
        tools={},
        orchestrations={
            "test": OrchestrationDefinition(
                flow=[StepInput(agent="worker", input="task")]
            )
        },
    )

    builder = GraphBuilder(config)
    graph = builder.build("test")

    agent_nodes = [n for n in graph.nodes.values() if n.node_type == NodeType.AGENT]
    assert len(agent_nodes) == 1
    assert agent_nodes[0].config.get("spawn_context") is None


def test_loop_node_no_cycle():
    """Test that loop nodes do NOT create a back-edge cycle from body to loop node."""
    from elasticity.config.schema import Config, AgentTypeDefinition, OrchestrationDefinition, LoopStep, StepInput
    
    # Create a minimal config with a loop
    minimal_config = Config(
        agent_types={
            "test_agent": AgentTypeDefinition(
                model="openai/gpt-4o",
                system_prompt="Test agent",
            )
        },
        tools={},
        orchestrations={
            "test_loop": OrchestrationDefinition(
                flow=[
                    LoopStep(
                        loop={
                            "max_iterations": 3,
                            "body": [
                                StepInput(agent="test_agent", input="Test", output_as="result")
                            ]
                        }
                    )
                ]
            )
        }
    )

    builder = GraphBuilder(minimal_config)
    graph = builder.build("test_loop")

    # Find loop node
    loop_node = next(n for n in graph.nodes.values() if n.node_type == NodeType.LOOP)
    assert loop_node is not None

    # Get the body entry node
    body_entry_id = loop_node.children[0]
    assert body_entry_id is not None

    # Traverse the body chain to find the last node
    last_body_node_id = body_entry_id
    while last_body_node_id in graph.nodes:
        node = graph.nodes[last_body_node_id]
        if node.next is None:
            break
        last_body_node_id = node.next

    # Verify the last body node does NOT point back to the loop node
    last_body_node = graph.nodes[last_body_node_id]
    assert last_body_node.next != loop_node.node_id, "Loop body should not create a cycle back to loop node"
