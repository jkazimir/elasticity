"""Tests for the conductor listing and detail API endpoints.

Covers:
  - GET /api/conductors — flat list of conductor configs (AC-1, AC-2, AC-5)
  - GET /api/conductors/{config_id} — conductor detail with teams (AC-3)
  - Regression: GET /api/orchestrations excludes conductor files (AC-4)
  - Edge cases: empty dir, malformed YAML, path traversal
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from elasticity.web.server import create_app

# ---------------------------------------------------------------------------
# Shared YAML fixtures
# ---------------------------------------------------------------------------

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
    description: Researches topics and returns a report.
    input:
      topic: string
    output: report
  writing:
    config: ./writing_team.yaml
    orchestration: main
    description: Writes polished content.
    input:
      research: string
"""

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

MALFORMED_YAML = "{ unclosed: ["


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conductor_only_dir(tmp_path: Path) -> Path:
    """Config dir containing one valid conductor YAML and nothing else."""
    (tmp_path / "my_conductor.yaml").write_text(CONDUCTOR_YAML)
    return tmp_path


@pytest.fixture()
def mixed_dir(tmp_path: Path) -> Path:
    """Config dir with one conductor YAML and one orchestration YAML."""
    (tmp_path / "my_conductor.yaml").write_text(CONDUCTOR_YAML)
    (tmp_path / "my_orch.yaml").write_text(ORCHESTRATION_YAML)
    return tmp_path


@pytest.fixture()
def orchestration_only_dir(tmp_path: Path) -> Path:
    """Config dir with only orchestration YAMLs — no conductor files."""
    (tmp_path / "my_orch.yaml").write_text(ORCHESTRATION_YAML)
    return tmp_path


@pytest.fixture()
def empty_dir(tmp_path: Path) -> Path:
    """Config dir with no YAML files at all."""
    return tmp_path


@pytest.fixture()
def malformed_dir(tmp_path: Path) -> Path:
    """Config dir with a malformed YAML that looks like a conductor file."""
    (tmp_path / "bad.yaml").write_text(MALFORMED_YAML)
    return tmp_path


# ---------------------------------------------------------------------------
# GET /api/conductors — list endpoint
# ---------------------------------------------------------------------------


class TestListConductors:
    def test_returns_200_with_conductor_list(self, conductor_only_dir: Path) -> None:
        client = TestClient(create_app(conductor_only_dir))
        resp = client.get("/api/conductors")
        assert resp.status_code == 200

    def test_single_conductor_entry_shape(self, conductor_only_dir: Path) -> None:
        """AC-2: exactly N entries returned, each with the expected fields."""
        client = TestClient(create_app(conductor_only_dir))
        data = client.get("/api/conductors").json()
        assert len(data) == 1

        entry = data[0]
        assert entry["config_id"] == "my_conductor"
        assert entry["config_filename"] == "my_conductor.yaml"
        assert entry["name"] == "my_conductor"
        assert entry["agent"] == "boss"
        assert entry["team_count"] == 2

    def test_mixed_dir_returns_only_conductor(self, mixed_dir: Path) -> None:
        """AC-2: orchestration files must not appear in the conductors list."""
        client = TestClient(create_app(mixed_dir))
        data = client.get("/api/conductors").json()
        config_ids = [e["config_id"] for e in data]
        assert "my_conductor" in config_ids
        assert "my_orch" not in config_ids

    def test_empty_list_when_no_conductors(self, orchestration_only_dir: Path) -> None:
        """AC-5: endpoint returns [] — no section should be rendered client-side."""
        client = TestClient(create_app(orchestration_only_dir))
        data = client.get("/api/conductors").json()
        assert data == []

    def test_empty_list_for_empty_dir(self, empty_dir: Path) -> None:
        """AC-5: endpoint returns [] for an empty config directory."""
        client = TestClient(create_app(empty_dir))
        data = client.get("/api/conductors").json()
        assert data == []

    def test_malformed_conductor_silently_skipped(self, malformed_dir: Path) -> None:
        """Malformed YAML is silently skipped — no 500 error."""
        client = TestClient(create_app(malformed_dir))
        resp = client.get("/api/conductors")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_no_duplicate_when_yaml_and_yml_both_exist(self, tmp_path: Path) -> None:
        """Stem deduplication: if both .yaml and .yml exist, only one appears."""
        (tmp_path / "cond.yaml").write_text(CONDUCTOR_YAML)
        (tmp_path / "cond.yml").write_text(CONDUCTOR_YAML)
        client = TestClient(create_app(tmp_path))
        data = client.get("/api/conductors").json()
        # Only one entry for stem "cond".
        assert len(data) == 1

    def test_multiple_conductors(self, tmp_path: Path) -> None:
        """AC-2: all N conductors are returned, none omitted."""
        (tmp_path / "conductor_a.yaml").write_text(CONDUCTOR_YAML)
        (tmp_path / "conductor_b.yaml").write_text(CONDUCTOR_YAML)
        client = TestClient(create_app(tmp_path))
        data = client.get("/api/conductors").json()
        assert len(data) == 2
        ids = {e["config_id"] for e in data}
        assert ids == {"conductor_a", "conductor_b"}


# ---------------------------------------------------------------------------
# GET /api/conductors/{config_id} — detail endpoint
# ---------------------------------------------------------------------------


class TestGetConductor:
    def test_returns_200_with_detail(self, conductor_only_dir: Path) -> None:
        client = TestClient(create_app(conductor_only_dir))
        resp = client.get("/api/conductors/my_conductor")
        assert resp.status_code == 200

    def test_detail_shape(self, conductor_only_dir: Path) -> None:
        client = TestClient(create_app(conductor_only_dir))
        data = client.get("/api/conductors/my_conductor").json()
        assert data["id"] == "my_conductor"
        assert data["filename"] == "my_conductor.yaml"
        assert data["agent"] == "boss"
        assert len(data["teams"]) == 2

    def test_team_fields(self, conductor_only_dir: Path) -> None:
        """Each team entry must include name, description, orchestration, config, input."""
        client = TestClient(create_app(conductor_only_dir))
        data = client.get("/api/conductors/my_conductor").json()
        teams_by_name = {t["name"]: t for t in data["teams"]}

        research = teams_by_name["research"]
        assert research["description"] == "Researches topics and returns a report."
        assert research["orchestration"] == "main"
        assert research["config"] == "./research_team.yaml"
        assert research["input"] == {"topic": "string"}
        assert research["output"] == "report"

        writing = teams_by_name["writing"]
        assert writing["input"] == {"research": "string"}
        assert writing["output"] is None

    def test_404_for_nonexistent_id(self, conductor_only_dir: Path) -> None:
        client = TestClient(create_app(conductor_only_dir))
        resp = client.get("/api/conductors/does_not_exist")
        assert resp.status_code == 404

    def test_404_when_id_is_an_orchestration(self, mixed_dir: Path) -> None:
        """AC-3: navigating to an orchestration file via conductor route → 404."""
        client = TestClient(create_app(mixed_dir))
        resp = client.get("/api/conductors/my_orch")
        assert resp.status_code == 404

    def test_400_on_path_traversal(self, conductor_only_dir: Path) -> None:
        client = TestClient(create_app(conductor_only_dir))
        resp = client.get("/api/conductors/../../etc/passwd")
        assert resp.status_code in (400, 404)


# ---------------------------------------------------------------------------
# AC-4 regression: orchestrations endpoint unaffected by conductor files
# ---------------------------------------------------------------------------


class TestOrchestrationsRegressionAC4:
    def test_orchestrations_excludes_conductor_files(self, mixed_dir: Path) -> None:
        """Conductor files must never appear in the orchestrations listing."""
        client = TestClient(create_app(mixed_dir))
        data = client.get("/api/orchestrations").json()
        config_ids = [o["config_id"] for o in data]
        assert "my_conductor" not in config_ids

    def test_orchestrations_still_present(self, mixed_dir: Path) -> None:
        """Orchestration entries remain fully intact after adding conductor support."""
        client = TestClient(create_app(mixed_dir))
        data = client.get("/api/orchestrations").json()
        assert len(data) >= 1
        names = [o["name"] for o in data]
        assert "main" in names

    def test_orchestrations_unchanged_without_conductors(
        self, orchestration_only_dir: Path
    ) -> None:
        """Baseline: /api/orchestrations is identical regardless of conductor presence."""
        # Get response with only orchestration files.
        client_orch = TestClient(create_app(orchestration_only_dir))
        baseline = client_orch.get("/api/orchestrations").json()

        # Copy same orch file into a mixed dir — add a conductor alongside it.
        import shutil, tempfile
        with tempfile.TemporaryDirectory() as mixed:
            mixed_path = Path(mixed)
            shutil.copy(
                orchestration_only_dir / "my_orch.yaml",
                mixed_path / "my_orch.yaml",
            )
            (mixed_path / "my_conductor.yaml").write_text(CONDUCTOR_YAML)
            client_mixed = TestClient(create_app(mixed_path))
            with_conductor = client_mixed.get("/api/orchestrations").json()

        assert baseline == with_conductor
