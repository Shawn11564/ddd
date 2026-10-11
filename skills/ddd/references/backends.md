# Doc backends

The script never talks to Google itself. It works on text. For Google Docs you
fetch the text with whatever MCP the host has connected and hand the script a
file. That keeps the skill portable across hosts and free of credentials.

## Local Markdown

`config.doc = {"type": "markdown", "path": "docs/plan.md"}` (repo-relative).

- Read: `ddd diff` reads the file itself.
- Write: `ddd status-block --write docs/plan.md` replaces only the status
  section (or appends it at the end). It refuses with exit 3 if the doc changed
  since the last accepted sync.
- Comments: none. The status section is the only channel.
- Editors that hold the file open (IDEs) normally reload it. If the developer
  has unsaved edits, their next save wins; the next sync then sees the doc
  unchanged apart from the status section, which is harmless.

## Google Docs through an MCP

`config.doc = {"type": "gdoc", "url": "...", "id": "<document id>"}`. `init`
extracts the id from the URL.

MCP servers differ in tool names and abilities, so detect capabilities from the
tools' descriptions rather than assuming names. Record what you find once, so
later runs don't re-probe:

```
ddd config set capabilities.read true
ddd config set capabilities.targeted_edit false
ddd config set capabilities.comments true
```

### Reading

Look for a tool that reads or exports a Drive file or a Google Doc by id or URL.
Prefer Markdown output: headings and checkboxes survive, and the parser handles
Google's Markdown export (escaped `\[next\]`, `\{#tag\}`, bold headings,
`☐`/`☑` checklists). Plain text works for checkbox tasks, tags and markers, but
headings flatten into description lines, so tell the developer that features
are only recognised from Markdown.

Save the text exactly as returned to `.ddd/tmp/doc.md` (the folder is
gitignored) and run `ddd diff .ddd/tmp/doc.md`. If no connected tool can read
the doc, stop and tell the developer. Suggest connecting a Google Drive or
Workspace MCP, or switching to a Markdown doc in the repo
(`ddd init --doc docs/plan.md --force`).

### Writing the status section: partial edits only

`capabilities.targeted_edit` is true only if a tool can change part of a
document: replace a text range, find-and-replace, Docs `batchUpdate`, or insert
at the end. Then:

1. Re-read the doc, save it to `.ddd/tmp/doc.md`, and run `ddd diff .ddd/tmp/doc.md`.
   If `doc_unchanged` is false, finish the sync (accept, with questions if
   needed) before writing.
2. `ddd status-block` gives the section text.
3. Replace the existing section, from the `DDD Status` heading up to the next
   heading of the same or higher level (or the end of the doc). If there is no
   section yet, append it at the end.

Many Drive connectors only offer "update file", which replaces the entire
content. **Do not use that on the developer's doc.** It drops formatting,
comments' anchors and suggestions, and anything typed since you read it. Set
`targeted_edit` to false and rely on comments, or on `.ddd/` alone. Say so once.

### Comments

`capabilities.comments` is true when a tool can create comments on a Drive file
(replies are a bonus). Then:

- Post one comment per state change per item, quoting the item's title (anchor
  it to that text if the tool allows): `[ddd] {#login} awaiting review: branch
  ddd/login, tests pass (npm test)`.
- Questions: `[ddd] question on "Export reports": CSV only, or CSV and XLSX?`
- Record each comment: `ddd set ID --comment-id <id> --comment-kind status|question`.
  The ids in `items.json` let you reply in the same thread next time instead of
  starting a new one.
- If the tool can list comments, read replies to open question comments at the
  start of each sync and treat them as the developer's answers. Resolve or close
  a question thread once you act on it, if the tool allows.
- Never use comments to edit or suggest changes to the developer's text.

### Without partial edits and without comments

The doc is read-only to you. Status lives in `.ddd/` and in your chat report.
Tell the developer once, and suggest an MCP with comment support if they want
status in the doc.
