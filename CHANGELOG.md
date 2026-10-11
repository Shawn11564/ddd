# Changelog

## 0.1.0 (unreleased)

First version.

- Menu, auto, status and setup modes.
- Initialize mode: surveys the project (`ddd survey`) and drafts the first plan,
  as a Markdown file or a Google Doc, then sets up.
- Local Markdown and Google Docs (through any connected MCP) as the planning doc.
- Item identity across reformats: tags, identical text, similarity scoring;
  uncertain matches, splits and merges go to the developer.
- `.ddd/` state: snapshot, item map, hot-item signal, audit log.
- One branch per item, `DDD-Item` commit trailers, merge and squash-merge detection.
- Agent-owned status section with concurrent-edit guard; comments when available.
