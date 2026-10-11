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

Use the same read tool every time: two tools render the same doc differently,
and switching makes unchanged items look edited. Record which one you use the
first time (`ddd config set capabilities.read "<tool>, <format>"`).

Known behaviour of the Google Drive connector (`download_file_content`,
`read_file_content`), checked on a real 75-item plan:

- `download_file_content` with `exportMimeType: text/markdown` is the faithful
  read: every heading, checkbox, tag and marker. It returns the Markdown
  **base64-encoded**, in JSON (`{"content": "..."}`).
- `read_file_content` returns readable text but drops checkboxes and code
  spans: only 21 of the 75 items parsed. Don't use it for syncing.

Getting the text to the script costs output tokens, because you write it to a
file. Keep it as low as you can:

1. **Skip the fetch when nothing changed.** Get the doc's `modifiedTime` from
   the metadata tool (`get_file_metadata` with `excludeContentSnippets: true`)
   and run `ddd diff --doc-version <modifiedTime>`. `fetch_skipped: true` means
   the doc is unchanged since the last sync. Pass the same `--doc-version` with
   the file when you do fetch, so the version is recorded. Comments and your
   own status writes also change `modifiedTime`; the next sync then fetches
   once, sees no change, and records the new version.
2. **Use a saved result as is.** When the host saved a large tool result to a
   file, pass that file: `ddd diff <file>` and `ddd parse <file>` read a saved
   JSON result (`{"content": ...}`, base64 or text, also inside a
   `[{"type": "text", "text": ...}]` list) and `.b64` files. Never retype it.
3. **Otherwise write what you received**: the base64 string to
   `.ddd/tmp/doc.b64`, or Markdown text to `.ddd/tmp/doc.md`. Base64 costs
   about three times the tokens of the same text (25K output tokens for a
   20 KB doc), which is why skipping the fetch matters.

The folder is gitignored. Run `ddd diff <file> --doc-version <modifiedTime>`.
If no connected tool can read the doc, stop and tell the developer. Suggest
connecting a Google Drive or Workspace MCP, or switching to a Markdown doc in
the repo (`ddd init --doc docs/plan.md --force`).

### Creating a doc (Initialize)

Upload the Markdown with conversion to a Google Doc (Drive `create_file` with
`contentMimeType: text/markdown`). The importer, checked by real round trips
([tests/fixtures/gdoc-import](https://github.com/Shawn11564/ddd/tree/main/tests/fixtures/gdoc-import)):

- keeps headings, checkboxes (`- [ ]`, `- [x]`), task tags, `Done when:` lines
  and markers, escaping them in the export (`{\#tag}`, `\[next\]`);
- **cuts a heading at its first `{`**: `## Finish Clash {#clash} [next]`
  becomes `## Finish Clash`, losing the tag and the marker, even if escaped.
  Put a heading's tag and markers on their own line under it
  ([doc-format.md](doc-format.md#markers)).

Create the doc once. If the read-back shows a loss, report it; never create a
second copy to retry, and never trash a file the developer didn't ask you to
delete.

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
