# Repository agent instructions

<!-- command-center:test-safety:v2 -->
## Test safety

- Prefer non-GUI compilation, static checks, unit tests, and headless test modes when they provide enough verification.
- Tests that open a GUI must be headless or hidden before the first top-level window is created.
- Never launch, focus, restore, maximize, or reveal a GUI test application unless the user explicitly requests a visible run.
- Delayed minimization is not background-safe: a visible window can disrupt an exclusive-fullscreen application before it is minimized.
- Automated tests must not play audio on the machine they run on; mute the application under test, never the user's system.

<!-- command-center:debugging:v1 -->
## Debugging

- Reproduce before you fix: write a check that fails for the reason the user sees, on the real code path (headless browser, the project's harness, a fixture server). Then fix until it passes. If you cannot make it fail, say so.
- Fix the layer that loses the state (the host that hides, remounts or reloads, the refresh that re-renders), even when it lives in a sibling repo. A downstream workaround (detect-and-restore, replaying from storage, retries) is a last resort, and say so when you use one.
- List every cause of the symptom, not only the trigger the user named, and check each one.
- Test the real thing. A simulation of your own theory (events fired by hand, a homemade host) proves the theory, not the fix. Background browser tabs throttle timers and events, so use a headless browser with a fake clock.
- A bug reported again means the last fix was wrong. Start from the evidence, and never blame stale code or a missing reload without proving it.
- Prove the fix runs where the user runs it (installed binary, the file actually served), and leave the check behind as a named regression test.

## This repository

- Run all checks with `python -m unittest discover -s tests -v` (headless; creates throwaway git repos in the temp dir).
- `skills/ddd/` is the shipped skill. Keep `SKILL.md` under 500 lines and its frontmatter within the Agent Skills spec (`tests/test_skill.py` checks this).
- Matching changes must keep `evals/matching/` green; add a scenario for every matching bug you fix.
- Every mode in the `SKILL.md` mode table needs a `commands/<mode>.md` (Claude Code's `/ddd:<mode>`). After changing `commands/` or the installers, also run `python evals/claude_code_commands.py` (needs the `claude` CLI).
