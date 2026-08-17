"""thinkcap — per-task reasoning-budget governor for Qwen3.8 coding agents.

m1 ships the profiler (``thinkcap probe``): it runs a fixed task suite against
a local llama.cpp server, parses every ``<|im_start|>think`` block from the
streamed response, counts reasoning tokens via the server's ``/tokenize``
endpoint (with an honest char-based fallback), and emits a per-task-class rich
table plus a ``policy.json`` the m2 governor will enforce.
"""
from __future__ import annotations

from .config import VERSION

__all__ = ["VERSION"]
__version__ = VERSION
