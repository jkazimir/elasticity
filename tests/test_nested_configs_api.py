"""Tests for nested subdirectory config discovery in the web API.

Covers:
  - GET /api/orchestrations discovers configs in subdirectories
  - GET /api/conductors discovers conductors in subdirectories
  - GET /api/configs/{config_id} resolves ~-encoded subdirectory config IDs
  - GET /api/conductors/{config_id} resolves ~-encoded subdirectory config IDs
  - Path traversal is still rejected after the encoding change
  - .yaml / .yml deduplication works for nested files
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from elasticity.web.server import create_app

# ---------------------------------------------------------------------------
# Shared YAML fixtures
# ---------------------------------------------------------------------------

ORCHESTRATION_YAML = """\
agent_types:
  worker:
    model: anthropic/claude-opus-4-5
    system_prompt: You are a worker.

tools: {}

orchestrations:
  main:
    mode: batch
    input:
      topic: string
    flow:
      - step: worker
        agent: worker
"""

CONDUCTOR_YAML = """\
agent_types:
  boss:
    model: anthropic/claude-opus-4-5
    system_prompt: You are the boss.

conductor:
  agent: boss

teams:
  research:
    config: ./research_team.yaml
    orchestration: main
    description: Researches topics.
    input:
      topic: string
    output: report
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def nested_orch_dir(tmp_path: Path) -> Path:
    """Config dir with an orchestration YAML one level deep in a subdirectory."""
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "my_orch.yaml").write_text(ORCHESTRATION_YAML)
    return tmp_path


@pytest.fixture()
def nested_conductor_dir(tmp_path: Path) -> Path:
    """Config dir with a conductor YAML one level deep in a subdirectory."""
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "my_conductor.yaml").write_text(CONDUCTOR_YAML)
    return tmp_path


@pytest.fixture()
def deeply_nested_dir(tmp_path: Path) -> Path:
    """Config dir with an orchestration two levels deep."""
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "deep_orch.yaml").write_text(ORCHESTRATION_YAML)
    return tmp_path


@pytest.fixture()
def mixed_nested_dir(tmp_path: Path) -> Path:
    """Config dir with both flat and nested configs."""
    (tmp_path / "flat_orch.yaml").write_text(ORCHESTRATION_YAML)
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "nested_orch.yaml").write_text(ORCHESTRATION_YAML)
    return tmp_path


# ---------------------------------------------------------------------------
# Discovery: /api/orchestrations
# ---------------------------------------------------------------------------


class TestNestedOrchestrationDiscovery:
    def test_nested_orch_appears_in_list(self, nested_orch_dir: Path) -> None:
        client = TestClient(create_app(nested_orch_dir))
        data = client.get("/api/orchestrations").json()
        config_ids = [o["config_id"] for o in data]
        assert "subdir~my_orch" in config_ids

    def test_nested_orch_config_id_uses_tilde_separator(
        self, nested_orch_dir: Path
    ) -> None:
        client = TestClient(create_app(nested_orch_dir))
        data = client.get("/api/orchestrations").json()
        entry = next(o for o in data if o["config_id"] == "subdir~my_orch")
        assert entry["name"] == "main"
        assert entry["config_filename"] == "my_orch.yaml"

    def test_deeply_nested_orch_uses_multiple_tildes(
        self, deeply_nested_dir: Path
    ) -> None:
        client = TestClient(create_app(deeply_nested_dir))
        data = client.get("/api/orchestrations").json()
        config_ids = [o["config_id"] for o in data]
        assert "a~b~deep_orch" in config_ids

    def test_flat_and_nested_both_discovered(self, mixed_nested_dir: Path) -> None:
        client = TestClient(create_app(mixed_nested_dir))
        data = client.get("/api/orchestrations").json()
        config_ids = {o["config_id"] for o in data}
        assert "flat_orch" in config_ids
        assert "subdir~nested_orch" in config_ids

    def test_yaml_yml_dedup_for_nested_file(self, tmp_path: Path) -> None:
        """Both .yaml and .yml for the same nested path count as one entry."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "orch.yaml").write_text(ORCHESTRATION_YAML)
        (subdir / "orch.yml").write_text(ORCHESTRATION_YAML)
        client = TestClient(create_app(tmp_path))
        data = client.get("/api/orchestrations").json()
        ids = [o["config_id"] for o in data]
        assert ids.count("subdir~orch") == 1


# ---------------------------------------------------------------------------
# Discovery: /api/conductors
# ---------------------------------------------------------------------------


class TestNestedConductorDiscovery:
    def test_nested_conductor_appears_in_list(self, nested_conductor_dir: Path) -> None:
        client = TestClient(create_app(nested_conductor_dir))
        data = client.get("/api/conductors").json()
        config_ids = [c["config_id"] for c in data]
        assert "subdir~my_conductor" in config_ids

    def test_nested_conductor_entry_shape(self, nested_conductor_dir: Path) -> None:
        client = TestClient(create_app(nested_conductor_dir))
        data = client.get("/api/conductors").json()
        entry = next(c for c in data if c["config_id"] == "subdir~my_conductor")
        assert entry["config_filename"] == "my_conductor.yaml"
        assert entry["agent"] == "boss"
        assert entry["team_count"] == 1


# ---------------------------------------------------------------------------
# Detail: /api/configs/{config_id}
# ---------------------------------------------------------------------------


class TestNestedConfigDetail:
    def test_get_config_by_tilde_id(self, nested_orch_dir: Path) -> None:
        client = TestClient(create_app(nested_orch_dir))
        resp = client.get("/api/configs/subdir~my_orch")
        assert resp.status_code == 200

    def test_get_config_detail_shape(self, nested_orch_dir: Path) -> None:
        client = TestClient(create_app(nested_orch_dir))
        data = client.get("/api/configs/subdir~my_orch").json()
        assert data["id"] == "subdir~my_orch"
        assert data["filename"] == "my_orch.yaml"
        assert len(data["orchestrations"]) == 1
        assert data["orchestrations"][0]["name"] == "main"

    def test_404_for_nonexistent_nested_id(self, nested_orch_dir: Path) -> None:
        client = TestClient(create_app(nested_orch_dir))
        resp = client.get("/api/configs/subdir~does_not_exist")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Detail: /api/conductors/{config_id}
# ---------------------------------------------------------------------------


class TestNestedConductorDetail:
    def test_get_conductor_by_tilde_id(self, nested_conductor_dir: Path) -> None:
        client = TestClient(create_app(nested_conductor_dir))
        resp = client.get("/api/conductors/subdir~my_conductor")
        assert resp.status_code == 200

    def test_get_conductor_detail_shape(self, nested_conductor_dir: Path) -> None:
        client = TestClient(create_app(nested_conductor_dir))
        data = client.get("/api/conductors/subdir~my_conductor").json()
        assert data["id"] == "subdir~my_conductor"
        assert data["filename"] == "my_conductor.yaml"
        assert data["agent"] == "boss"


# ---------------------------------------------------------------------------
# Path traversal protection still works
# ---------------------------------------------------------------------------


class TestPathTraversalProtection:
    def test_dotdot_segment_rejected(self, nested_orch_dir: Path) -> None:
        client = TestClient(create_app(nested_orch_dir))
        resp = client.get("/api/configs/..~etc~passwd")
        assert resp.status_code == 400

    def test_dot_prefix_segment_rejected(self, nested_orch_dir: Path) -> None:
        client = TestClient(create_app(nested_orch_dir))
        resp = client.get("/api/configs/.hidden~file")
        assert resp.status_code == 400

    def test_empty_segment_rejected(self, nested_orch_dir: Path) -> None:
        client = TestClient(create_app(nested_orch_dir))
        resp = client.get("/api/configs/subdir~~file")
        assert resp.status_code == 400

    def test_plain_traversal_still_rejected(self, nested_orch_dir: Path) -> None:
        """Old-style slash-based traversal still returns 404 (FastAPI strips it)."""
        client = TestClient(create_app(nested_orch_dir))
        # FastAPI will 404 on unknown routes rather than pass them to the handler
        resp = client.get("/api/configs/../../etc/passwd")
        assert resp.status_code in (400, 404)
