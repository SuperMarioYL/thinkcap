"""What using thinkcap looks like — the 3-command happy path, runnable.

Run from the repo root after `pip install -e .`:

    python examples/happy_path.py

It invokes the real CLI in --mock mode (no live llama.cpp needed) so the
happy path is reproducible anywhere. Drop `--mock` and point `--server` at
your llama.cpp to profile the real Qwen3.8 think trace.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

POLICY = Path("/tmp/thinkcap-policy.json")
BUDGETS = Path("/tmp/thinkcap-budgets.json")


def main() -> None:
    cmd = [
        sys.executable, "-m", "thinkcap.cli", "probe",
        "--mock",
        "--policy", str(POLICY),
        "--budgets", str(BUDGETS),
    ]
    print("$ " + " ".join(cmd[2:]))
    subprocess.run(cmd, check=True)
    print(f"\nwrote {POLICY} ({POLICY.stat().st_size} bytes) — the per-class budget m2 will enforce.")


if __name__ == "__main__":
    main()
