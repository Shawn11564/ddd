"""Check that a real Claude Code lists every ddd mode as a slash command.

    python evals/claude_code_commands.py

Needs the `claude` CLI on PATH and logged in. For each install layout it starts
Claude Code headless, reads only the startup (init) event, which lists the
registered slash commands, and stops the process before any model call.

- plugin:  `--plugin-dir <repo>` must register ddd:ddd and ddd:<mode>
- project: commands copied to .claude/commands/ddd/ (what `install.sh --target
  project` and `--target claude` do) must register ddd:<mode>

Exit 0 when every mode is registered in both layouts. Not part of the unit
tests: it needs the CLI and a login, so run it after changing commands/ or the
installers.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMANDS = os.path.join(REPO, "commands")


def slash_commands(cwd, extra):
    claude = shutil.which("claude")
    if not claude:
        sys.exit("claude CLI not found on PATH")
    argv = [claude, "-p", "--output-format", "stream-json", "--verbose", "--max-turns", "1"] + extra + ["ok"]
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for raw in proc.stdout:
            event = json.loads(raw.decode("utf-8"))
            if event.get("type") == "system" and event.get("subtype") == "init":
                return set(event.get("slash_commands", []))
        sys.exit("no init event: " + proc.stderr.read().decode("utf-8", "replace")[:500])
    finally:
        proc.kill()
        proc.wait()


def main():
    modes = sorted(fn[:-3] for fn in os.listdir(COMMANDS) if fn.endswith(".md"))
    expected = {"ddd:" + m for m in modes}
    failed = False
    with tempfile.TemporaryDirectory(prefix="ddd-cc-") as tmp:
        plugin = slash_commands(tmp, ["--plugin-dir", REPO])
        project_dir = os.path.join(tmp, "project")
        shutil.copytree(COMMANDS, os.path.join(project_dir, ".claude", "commands", "ddd"))
        project = slash_commands(project_dir, [])
    for layout, got, want in (("plugin", plugin, expected | {"ddd:ddd"}), ("project", project, expected)):
        missing = sorted(want - got)
        print("%-8s %s" % (layout, "ok" if not missing else "missing " + ", ".join(missing)))
        failed = failed or bool(missing)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
