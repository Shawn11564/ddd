# Changelog

## 0.1.0 (unreleased)

First version.

- Menu, auto, status and setup modes.
- Initialize mode: surveys the project (`ddd survey`) and drafts the first plan,
  as a Markdown file or a Google Doc, then sets up.
- Fixes from the first real Initialize run (a Google Doc):
  - A heading's tag and markers can sit on their own line under it. Google's
    Markdown import cuts a heading at its first `{`, so this is the layout for
    Google Docs. Verified with real round trips (`tests/fixtures/gdoc-import`).
  - `diff --doc-version`: skip fetching a Google Doc whose modified time hasn't
    changed since the last sync. A fetch used to cost ~25K output tokens for a
    20 KB doc.
  - `diff` and `parse` read a saved connector result (JSON, base64) and `.b64`
    files directly.
  - `parse --outline` and `parse --compare` for validating drafts and conversions.
  - `init`/`config set test_command` warn when the program is in the project
    root but not on PATH (`gradlew` instead of `./gradlew`).
  - `survey` reports `base_guess`/`on_base`; Initialize ticks only work on the
    base branch, creates a Google Doc once, never trashes files, and explains
    why `.ddd/` stays uncommitted off the base branch.
- Status section in a Google Doc through the Google Docs connector:
  `status-block --gdoc-read` builds the `batchUpdate` requests from a
  `read_doc` result, saved by the host or trimmed to the fields it reads
  (replace or append, revision-guarded, tab-aware, refuses over pending
  suggestions) and `--gdoc-check` verifies a fresh read. Checked on a live
  doc. `init` keeps the URL's `?tab=`.
- Initialize: arguments that name the format drop only that question; the
  one message still goes out when there is a roadmap to raise or the run is
  off the base branch (behaviour eval 12).
- Docs: Drive's `modifiedTime` lags edits by a minute or more, so a sync
  right after an edit may skip it; fetch anyway when the developer says they
  just edited.
- Claude Code commands per mode (`/ddd:menu`, `/ddd:auto`, `/ddd:status`,
  `/ddd:setup`, `/ddd:initialize`) so typing `/ddd:` completes them, with
  argument hints. Shipped in the plugin; the installers copy them too.
- Local Markdown and Google Docs (through any connected MCP) as the planning doc.
- Item identity across reformats: tags, identical text, similarity scoring;
  uncertain matches, splits and merges go to the developer.
- `.ddd/` state: snapshot, item map, hot-item signal, audit log.
- One branch per item, `DDD-Item` commit trailers, merge and squash-merge detection.
- Agent-owned status section with concurrent-edit guard; comments when available.
