"""Static configuration: task classes, the coding-mini suite, think delimiters.

Everything that is a *fixed* fact about the Qwen3.8 surface lives here so the
profiler, policy, and proxy all share one source of truth. The Qwen3.8 chat
template emits reasoning as a literal ``<|im_start|>think`` ... ``<|im_end|>``
block inside the streamed ``content`` deltas — that is the surface the whole
governor gates on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List

try:  # VERSION is written by the build; fall back for in-tree runs.
    from importlib.metadata import version as _pkg_version

    VERSION = _pkg_version("thinkcap")
except Exception:  # pragma: no cover - not installed
    VERSION = "0.1.0"


class TaskClass(str, Enum):
    """A coarse classification of the coding task in front of the model.

    The whole thesis is that a *global* ``--reasoning-budget`` is wrong because
    a rename and a refactor need different budgets. These classes are the
    axis the policy is learned over.
    """

    RENAME = "rename"
    BUGFIX = "bugfix"
    REFACTOR = "refactor"
    NEW_FILE = "new_file"
    DEFAULT = "default"

    @classmethod
    def coerce(cls, value: str | "TaskClass") -> "TaskClass":
        if isinstance(value, TaskClass):
            return value
        key = (value or "default").strip().lower().replace("-", "_")
        try:
            return cls(key)
        except ValueError:
            return cls.DEFAULT


# --- Qwen3.8 reasoning-trace surface ---------------------------------------

#: The literal token the Qwen3.8 chat template emits to open a think block.
THINK_START = "<|im_start|>think"
#: The literal token that closes a think block (and any assistant turn).
THINK_END = "<|im_end|>"

#: The inception thought injected mid-stream to force the model to act once a
#: live think trace crosses the per-task budget. This is ggerganov's pattern:
#: the model has already reasoned past the budget, so we hand it a thought
#: that says "stop reasoning, act now" and close the block.
DEFAULT_INCEPTION_TOKEN = (
    " ...I have reasoned enough for this task. Acting now.\n" + THINK_END
)


# --- Trace packs (per-model delimiter profiles) -----------------------------


@dataclass(frozen=True)
class TracePack:
    """The delimiter surface of one model family's reasoning trace.

    The governance primitive (parse the live think trace, gate on a
    per-task budget, inject an inception thought) is model-agnostic; only
    the delimiters and the token that closes a think block differ. A pack
    bundles those facts so ``probe`` and ``serve`` handle non-Qwen3.8
    streams with the same parser.
    """

    name: str
    label: str
    think_start: str
    think_end: str
    inception_token: str


#: Qwen3.8 — the v0.1.0 surface, byte-identical literals.
TRACE_PACK_QWEN38 = TracePack(
    name="qwen3.8",
    label="Qwen3.8 (<|im_start|>think ... <|im_end|>)",
    think_start=THINK_START,
    think_end=THINK_END,
    inception_token=DEFAULT_INCEPTION_TOKEN,
)

#: DeepSeek-R1 — reasoning arrives as a ``<think>`` ... ``</think>`` block
#: inside the content stream.
TRACE_PACK_DEEPSEEK_R1 = TracePack(
    name="deepseek-r1",
    label="DeepSeek-R1 (<think> ... </think>)",
    think_start="<think>",
    think_end="</think>",
    inception_token=" ...I have reasoned enough for this task. Acting now.\n</think>",
)

#: GLM-4.6-Z1 — same primitive, GLM's chat-template delimiters.
TRACE_PACK_GLM_46_Z1 = TracePack(
    name="glm-4.6-z1",
    label="GLM-4.6-Z1 (<think> ... </think>)",
    think_start="<think>",
    think_end="</think>",
    inception_token=" ...I have reasoned enough for this task. Acting now.\n</think>",
)

#: Registry of shippable trace packs, keyed by name (the ``--profile`` flag).
TRACE_PACKS: Dict[str, TracePack] = {
    TRACE_PACK_QWEN38.name: TRACE_PACK_QWEN38,
    TRACE_PACK_DEEPSEEK_R1.name: TRACE_PACK_DEEPSEEK_R1,
    TRACE_PACK_GLM_46_Z1.name: TRACE_PACK_GLM_46_Z1,
}

#: The default pack — unchanged v0.1.0 behavior.
DEFAULT_TRACE_PACK = TRACE_PACK_QWEN38


def get_trace_pack(name: str) -> TracePack:
    """Resolve a trace pack by name. Raises ``KeyError`` for unknown names."""
    pack = TRACE_PACKS.get(name)
    if pack is None:
        raise KeyError(
            f"unknown trace pack '{name}'; available: "
            f"{', '.join(sorted(TRACE_PACKS))}"
        )
    return pack


def available_trace_pack_names() -> List[str]:
    return sorted(TRACE_PACKS)


# --- Paths + defaults -------------------------------------------------------

DEFAULT_POLICY_PATH = Path("policy.json")
DEFAULT_BUDGET_STORE_PATH = Path("budgets.json")
DEFAULT_PORT = 8088
DEFAULT_UPSTREAM = "http://localhost:8080"
DEFAULT_MODEL = "qwen3.8-27b"
#: Fallback budget (tokens) for a task class with no observed profile yet.
DEFAULT_FALLBACK_BUDGET = 2048


# --- Task + suite definitions ---------------------------------------------


@dataclass(frozen=True)
class Task:
    """A single canned prompt in the measurement suite."""

    task_id: str
    task_class: TaskClass
    prompt: str
    expected_action: str

    def __post_init__(self) -> None:
        # dataclass(frozen=True) — coerce the class enum defensively.
        if not isinstance(self.task_class, TaskClass):
            object.__setattr__(self, "task_class", TaskClass.coerce(self.task_class))


@dataclass
class Suite:
    """A named bundle of tasks the profiler runs end-to-end."""

    name: str
    tasks: List[Task] = field(default_factory=list)


def coding_mini_suite() -> Suite:
    """The 5-task suite m1 ships with.

    Spanning the four real classes plus a default: a one-line rename, a
    bugfix, a refactor, a new-file scaffold, and a second rename — enough
    variance to produce a per-class p50/p90 without a 50-task marathon.
    """
    return Suite(
        name="coding-mini",
        tasks=[
            Task(
                "rename-01",
                TaskClass.RENAME,
                "Rename the local variable `foo` to `bar` in src/utils.py. "
                "Do not touch any other identifier.",
                "sed -i 's/\\bfoo\\b/bar/g' src/utils.py",
            ),
            Task(
                "bugfix-01",
                TaskClass.BUGFIX,
                "Fix the off-by-one in process(): the loop should be "
                "`for i in range(len(items))` not `range(len(items) + 1)`. "
                "Return the corrected function body only.",
                "edit loop bound to range(len(items))",
            ),
            Task(
                "refactor-01",
                TaskClass.REFACTOR,
                "Extract the request-validation block at the top of "
                "`handle_request` into a `validate(req) -> list[str]` "
                "helper. Keep behavior identical.",
                "extract validate() helper",
            ),
            Task(
                "newfile-01",
                TaskClass.NEW_FILE,
                "Create a new config.py exporting a `Settings` dataclass "
                "with two fields: `host: str` and `port: int`. Include "
                "the import and a default instance.",
                "write config.py with Settings dataclass",
            ),
            Task(
                "rename-02",
                TaskClass.RENAME,
                "Rename the function `do_thing` to `process_request` "
                "across handlers.py and tests/test_handlers.py.",
                "rename do_thing -> process_request",
            ),
        ],
    )


#: Registry of shippable suites, keyed by name (the ``--suite`` flag).
SUITES: Dict[str, Suite] = {
    "coding-mini": coding_mini_suite,
}


def get_suite(name: str) -> Suite:
    """Resolve a suite by name. Raises ``KeyError`` for unknown suites."""
    factory = SUITES.get(name)
    if factory is None:
        raise KeyError(
            f"unknown suite '{name}'; available: {', '.join(sorted(SUITES))}"
        )
    return factory()


def available_suite_names() -> List[str]:
    return sorted(SUITES)
