Output assets/demo.gif

Set Shell "bash"
Set Theme "Dracula"
Set FontSize 13
Set Width 100
Set Height 36
Set Padding 20

# v0.1 ships the profiler (thinkcap probe) and the dry-run proxy surface
# (thinkcap serve). The 10-minute happy path: install -> probe the think
# trace -> emit a per-task-class policy.json -> stand up the dry-run proxy.
# The tape uses --mock so it renders in CI without a live llama.cpp server;
# against your real server you drop the flag and see the 3,100 -> ~620
# before/after the README pitches.

# --- 1. install (the demo.yml workflow pre-installs with `pip install -e .`) ---
Type "pip install -e . 2>/dev/null | tail -1; echo 'thinkcap installed'"
Sleep 1s
Enter
Sleep 2s

# --- 2. version sanity ---
Type "thinkcap version"
Sleep 1s
Enter
Sleep 2s

# --- 3. probe: profile the think trace across the coding-mini suite ---
Type "thinkcap probe --mock --policy ./policy.json --budgets ./budgets.json"
Sleep 1s
Enter
Sleep 4s

# --- 4. inspect the learned per-class policy ---
Type "cat policy.json"
Sleep 1s
Enter
Sleep 2s

# --- 5. stand up the dry-run proxy + confirm it is healthy ---
Type "thinkcap serve --policy ./policy.json --dry-run --port 8088 >/tmp/thinkcap.log 2>&1 &"
Sleep 1s
Enter
Sleep 3s

Type "curl -s http://127.0.0.1:8088/health"
Sleep 1s
Enter
Sleep 2s

# --- 6. tear down + the closing note ---
Type "kill %1 2>/dev/null; echo 'proxy stopped (dry-run, advisory)'"
Sleep 1s
Enter
Sleep 2s

Type "# repoint your coding agent -> base_url=http://127.0.0.1:8088/v1"
Sleep 1s
Enter
Sleep 2s

Type "# governed turn (3,100 -> ~620 tokens, same diff) needs --accept (m3)"
Sleep 1s
Enter
Sleep 3s
