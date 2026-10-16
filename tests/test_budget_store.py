"""budgets.json corruption must never destroy accumulated observations.

v0.2.0 silently reset a corrupt store to empty (BudgetStore.load swallowed
json.JSONDecodeError) and let the next probe's save() overwrite the file —
the accumulated distribution, the whole point of the append-only store, was
destroyed with no warning and no backup. These tests pin the fixed contract:
the corrupt bytes are preserved as a ``.corrupt-<ns>`` sibling, probing
starts fresh, and if even the backup fails the store refuses to proceed.
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from thinkcap.budget_store import BudgetStore
from thinkcap.cli import app

runner = CliRunner()


def test_corrupt_budgets_file_is_backed_up_not_destroyed(tmp_path) -> None:
    budgets = tmp_path / "budgets.json"
    corrupt = '{"version":"0.2.0","records":[{"task_class":"ren'
    budgets.write_text(corrupt, encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "probe",
            "--mock",
            "--policy",
            str(tmp_path / "policy.json"),
            "--budgets",
            str(budgets),
        ],
    )

    assert result.exit_code == 0
    siblings = list(tmp_path.glob("budgets.json.corrupt-*"))
    assert len(siblings) == 1
    assert siblings[0].read_text(encoding="utf-8") == corrupt
    store = json.loads(budgets.read_text(encoding="utf-8"))
    # Exactly the new run's observations — the corrupt history is preserved
    # separately, not silently merged or destroyed.
    assert len(store["records"]) == 5


def test_corrupt_budgets_file_refuses_when_backup_fails(tmp_path, monkeypatch) -> None:
    budgets = tmp_path / "budgets.json"
    budgets.write_text("{broken", encoding="utf-8")

    def _fail(self, target):
        raise OSError("cannot rename")

    monkeypatch.setattr("pathlib.Path.rename", _fail)
    with pytest.raises(ValueError, match="corrupt"):
        BudgetStore(budgets).load()
    # The original bytes are untouched — no destructive overwrite happened.
    assert budgets.read_text(encoding="utf-8") == "{broken"


def test_readable_store_loads_unchanged(tmp_path) -> None:
    budgets = tmp_path / "budgets.json"
    budgets.write_text(
        json.dumps(
            {
                "version": "0.2.0",
                "records": [
                    {
                        "task_class": "rename",
                        "tokens": 620,
                        "outcome": "observed",
                        "ts": 1.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    profiles = BudgetStore(budgets).profiles()
    assert len(profiles) == 1
    assert profiles[0].observed_tokens == [620]
