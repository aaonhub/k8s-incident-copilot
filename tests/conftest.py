import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "eval" / "fixtures"


@pytest.fixture
def load():
    from copilot.cluster import load_snapshot
    from copilot.facts import Facts

    def _load(name: str) -> Facts:
        return Facts(load_snapshot(FIXTURES / f"{name}.json.gz"))

    return _load


@pytest.fixture(autouse=True)
def isolated_runs(tmp_path, monkeypatch):
    """Audit log / action DB go to a temp dir, never the real runs/."""
    from copilot import actions

    monkeypatch.setattr(actions, "RUNS", tmp_path)
    monkeypatch.setattr(actions, "AUDIT_LOG", tmp_path / "audit.jsonl")
    monkeypatch.setattr(actions, "ACTIONS_DB", tmp_path / "actions.sqlite")
    monkeypatch.setenv("COPILOT_LLM_MODE", "replay")
