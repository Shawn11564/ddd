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
   once, sees no change, and records the new version. `modifiedTime` trails
   edits by a minute or more (seen on a live doc), so a sync right after an
   edit can skip it; the next sync catches it, and writes stay safe because
   they are revision-guarded. If the developer says they just edited the doc,
   fetch anyway.
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

`capabilities.targeted_edit` is set only if a tool can change part of a
document. The Google Docs connector can: `read_doc` returns the document's
structure (`documents.get` JSON, with positions and a `revisionId`) and
`update_doc` applies Docs `batchUpdate` requests. The Google Drive connector
can't: its `update_file` only renames or moves a file. Record the tool, e.g.
`ddd config set capabilities.targeted_edit "update_doc"`.

With the Google Docs connector:

1. Finish the sync first: the diff must report `doc_unchanged` (accept, with
   questions if needed).
2. Read the structure with `read_doc` (no comments). If the host saved the
   result to a file, pass that file as is. If it came back inline (a small
   doc), write `.ddd/tmp/read.json` yourself with only the fields the script
   reads, copied exactly: `documentId`, `revisionId`, and per tab
   `tabProperties.tabId` and `documentTab.body.content`. Keep every `content`
   element, in order. A section break or table needs only `startIndex` and
   `endIndex`; a paragraph needs `startIndex`, `endIndex`,
   `paragraphStyle.namedStyleType`, `bullet` (`{}` when present) and, per
   element, the `textRun`'s `content`, `textStyle` and any
   `suggestedInsertionIds`/`suggestedDeletionIds`. Drop `lists`,
   `namedStyles`, `documentStyle` and the other style fields: they are most of
   the size. Never paraphrase text or recompute an index.
3. `ddd status-block --gdoc-read <saved read>` prints `requests` and
   `writeControl` (`requiredRevisionId` from that read). They replace the
   section, from the `DDD Status` heading up to the next heading of the same
   or higher level (or the end of the doc), or append it at the end. They keep
   the section's last paragraph break and restyle only the new paragraphs, so
   the developer's text and styles are untouched. The tab is `--tab`, else
   the doc URL's `?tab=`, else the first tab (a warning says so when there are
   several).
4. Send `requests` and `writeControl` in one `update_doc` call, with the
   document id. Never drop `writeControl`: if the call is rejected because the
   revision changed, the developer is editing; read again and rebuild, or skip
   the write this run.
5. Read again and run `ddd status-block --gdoc-check <saved read>`. `ok`
   logs the write; differences mean the edit didn't land as built, so report
   them and don't retry blindly.

Exit 3 from `--gdoc-read` means the section has pending suggestions; ask the
developer to accept or reject them first. The write changes the doc's
`modifiedTime` (after the lag above), so a later sync fetches once and
records the new version.

The requests follow the Docs API rules (positions in UTF-16 units, a
paragraph's style on its trailing newline, the body's last newline kept). Their
arithmetic is covered by `GdocStatusTests` in `tests/test_ddd.py`, against a
model of the API whose reads have the trimmed shape from step 2. One live run
(2026-10-11) confirmed append, replace before a developer heading, emoji
positions and the stale-revision rejection against Google itself; it also
showed the connector's read shape, `{"content": {...}}`, now in that test.

Other MCPs: use a range replace or find-and-replace on the same section, and
re-read to check the result.

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
Tell the developer once, and suggest connecting the Google Docs connector (or
another MCP that edits part of a doc or posts comments) if they want status in
the doc.
