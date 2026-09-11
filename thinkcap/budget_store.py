"""JSON-on-disk store for observed reasoning-token outcomes.

No database in v0.1 — one local file, one writer, single user. The store is
append-only at the record level so re-probing a class widens its
distribution; :meth:`BudgetStore.profiles` re-aggregates on read so the
policy is always built from the freshest observations.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List

from .config import DEFAULT_BUDGET_STORE_PATH, TaskClass
from .policy import ReasoningProfile


@dataclass
class OutcomeRecord:
    """One observed turn: class, tokens used, outcome tag, wall-clock.

    ``token_source`` records which counter produced the tokens
    (``"tokenize_api"`` or ``"approx"``) and ``trace_pack`` which model
    surface they were observed on, so re-aggregated profiles keep their
    provenance across runs and distributions from different packs never
    mix. Records written by v0.1.0 lack both fields and load back as
    ``"approx"`` on the default (qwen3.8) pack.
    """

    task_class: TaskClass
    tokens: int
    outcome: str = "observed"
    ts: float = field(default_factory=time.time)
    token_source: str = "approx"
    trace_pack: str = "qwen3.8"

    def to_dict(self) -> dict:
        return {
            "task_class": self.task_class.value,
            "tokens": int(self.tokens),
            "outcome": self.outcome,
            "ts": self.ts,
            "token_source": self.token_source,
            "trace_pack": self.trace_pack,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OutcomeRecord":
        return cls(
            task_class=TaskClass.coerce(d.get("task_class", "default")),
            tokens=int(d.get("tokens", 0)),
            outcome=str(d.get("outcome", "observed")),
            ts=float(d.get("ts", 0.0)),
            token_source=str(d.get("token_source", "approx")),
            trace_pack=str(d.get("trace_pack", "qwen3.8")),
        )


class BudgetStore:
    """A JSON-on-disk log of observed reasoning outcomes."""

    def __init__(self, path: Path | str = DEFAULT_BUDGET_STORE_PATH) -> None:
        self.path = Path(path)
        self._records: List[OutcomeRecord] = []
        self._loaded = False

    # -- IO ------------------------------------------------------------------

    def load(self) -> List[OutcomeRecord]:
        if not self.path.exists():
            self._records = []
            self._loaded = True
            return self._records
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._records = []
            self._loaded = True
            return self._records
        rows = data.get("records", []) if isinstance(data, dict) else data
        self._records = [OutcomeRecord.from_dict(r) for r in rows if isinstance(r, dict)]
        self._loaded = True
        return self._records

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": "0.1.0",
            "records": [r.to_dict() for r in self._records],
        }
        self.path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return self.path

    # -- mutation -----------------------------------------------------------

    def record(
        self,
        task_class: TaskClass | str,
        tokens: int,
        outcome: str = "observed",
        token_source: str = "approx",
        trace_pack: str = "qwen3.8",
    ) -> OutcomeRecord:
        if not self._loaded:
            self.load()
        rec = OutcomeRecord(
            task_class=TaskClass.coerce(task_class),
            tokens=int(tokens),
            outcome=outcome,
            token_source=token_source,
            trace_pack=trace_pack,
        )
        self._records.append(rec)
        return rec

    def extend(self, records: Iterable[OutcomeRecord]) -> None:
        if not self._loaded:
            self.load()
        self._records.extend(records)

    # -- aggregation --------------------------------------------------------

    def profiles(
        self, trace_pack: str | None = None
    ) -> List[ReasoningProfile]:
        """Re-aggregate the log into per-class :class:`ReasoningProfile`.

        ``trace_pack`` restricts the aggregation to observations recorded on
        that pack's surface, so budgets learned for one model are never
        widened by another model's distribution.
        """
        if not self._loaded:
            self.load()
        by_class: Dict[TaskClass, List[int]] = {}
        source_for: Dict[TaskClass, str] = {}
        for rec in self._records:
            if trace_pack is not None and rec.trace_pack != trace_pack:
                continue
            by_class.setdefault(rec.task_class, []).append(rec.tokens)
            # Same rule as profiler.aggregate: the class is labeled by the
            # most precise counter any of its observations used.
            if rec.token_source == "tokenize_api" or rec.task_class not in source_for:
                source_for[rec.task_class] = rec.token_source
        profs: List[ReasoningProfile] = []
        for cls, toks in sorted(by_class.items(), key=lambda kv: kv[0].value):
            p = ReasoningProfile(
                task_class=cls,
                observed_tokens=toks,
                token_source=source_for.get(cls, "approx"),
            )
            p.recompute()
            profs.append(p)
        return profs

    def all_records(self) -> List[OutcomeRecord]:
        if not self._loaded:
            self.load()
        return list(self._records)
