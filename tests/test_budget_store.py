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


# --- a valid-JSON store of the wrong shape must not be emptied ---------------


def test_wrong_shape_records_is_quarantined_not_overwritten(tmp_path) -> None:
    # fail-before (v0.3.0): load() only catches JSONDecodeError, so a
    # syntactically valid but non-store file (records is a string, e.g. a
    # partially mangled write) loads as [] and the next save() rewrites it —
    # the accumulated history is destroyed with no warning and no backup.
    budgets = tmp_path / "budgets.json"
    original = '{"version":"0.3.0","records":"truncated"}'
    budgets.write_text(original, encoding="utf-8")

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
    assert siblings[0].read_text(encoding="utf-8") == original
    assert len(json.loads(budgets.read_text(encoding="utf-8"))["records"]) == 5


def test_non_iterable_store_json_is_quarantined(tmp_path) -> None:
    # fail-before: `for r in rows` raised TypeError ('int' object is not
    # iterable) straight through the CLI.
    path = tmp_path / "budgets.json"
    path.write_text("42", encoding="utf-8")
    store = BudgetStore(path)
    assert store.load() == []
    assert len(list(tmp_path.glob("budgets.json.corrupt-*"))) == 1
    assert not path.exists()


def test_unreadable_store_raises_clean_error(tmp_path, monkeypatch) -> None:
    # fail-before: OSError was swallowed as "empty store", so a file that
    # could not be read looked like no history and could be overwritten later.
    path = tmp_path / "budgets.json"
    path.write_text('{"version": "0.3.0", "records": []}', encoding="utf-8")

    def _boom(self, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("pathlib.Path.read_text", _boom)
    with pytest.raises(ValueError, match="could not be read"):
        BudgetStore(path).load()
    # The bytes are untouched (read_bytes, not the patched read_text).
    assert b'"records"' in path.read_bytes()
