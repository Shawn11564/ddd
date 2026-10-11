# Google Docs import fixtures

Real round trips through Google Drive (2026-10-11): each `*.source.md` was
uploaded with `create_file` (`contentMimeType: text/markdown`, converted to a
Google Doc), then exported with `download_file_content`
(`exportMimeType: text/markdown`). `*.export.json` is the connector's result,
saved verbatim (base64 `content`).

- `placements`: which tag placements survive. Google cuts a heading at its
  first `{`: the tag and anything after it (markers, words) are lost, even
  escaped. A tag on its own line under the heading, task tags and markers
  without a `{` before them survive (escaped as `{\#tag}`, `\[next\]`).
- `layout`: the heading layout the skill recommends for Google Docs (tags and
  markers on the line under the heading). It parses identically before and
  after the round trip.

`tests/test_ddd.py` (`GoogleImportTests`) checks both. To refresh them, repeat
the round trip and replace the exports.
