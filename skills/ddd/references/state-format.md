# .ddd/ state and command reference

## Files

| Path | Committed | Content |
|---|---|---|
| `.ddd/config.json` | yes | Settings (below) |
| `.ddd/items.json` | yes | Every tracked item, keyed by id |
| `.ddd/snapshot.md` | yes | The doc text as of the last accepted sync; git history keeps older ones |
| `.ddd/log.jsonl` | yes | Append-only audit trail |
| `.ddd/README.md` | yes | One-paragraph explanation for humans |
| `.ddd/tmp/` | no (`.ddd/.gitignore`) | Fetched doc text, pending diff |

With several docs ([multi-doc.md](multi-doc.md)), `config.json` and
`log.jsonl` stay at `.ddd/` and each doc's `items.json`, `snapshot.md` and
`tmp/` move to `.ddd/docs/<name>/`. The first `ddd doc add` makes that move.

## config.json

| Key | Default | Meaning |
|---|---|---|
| `doc.type` | `markdown` | `markdown` or `gdoc` |
| `doc.path` / `doc.url` / `doc.id` | | Repo-relative path, or Google Docs URL and id |
| `status_section` | `DDD Status` | Heading of the agent-owned section |
| `base_branch` | current branch at init | Where item branches fork from and state is committed |
| `branch_prefix` | `ddd/` | Item branch prefix |
| `test_command` | null | Confirmed with the developer on first build |
| `hot_half_life_days` | 7 | Decay of the "hot" ranking signal |
| `auto.on_blocker` | `stop` | `stop` or `skip` in auto mode |
| `auto.max_items` | null | Default item limit for auto mode |
| `capabilities.read` / `.targeted_edit` / `.comments` | null | Cached MCP capabilities (Google Docs) |
| `doc.tab` | none | Google Doc tab to write the status section to, from the URL's `?tab=` |

Several docs: `doc` is replaced by `docs` {name: {type, path, url, id, tab,
scope, test_command, branch_prefix, status_section, former_names,
legacy_trailers}} and `doc_order`; `version` is 2. The doc's own settings win
over the repo-wide ones; `branch_prefix` defaults to `ddd/<name>/`.
`legacy_trailers` marks the doc that existed before (its bare trailers count).
`wait_for` (from `doc add --wait-for`) lists items whose leaving their own doc
the doc's first sync waits for; it is removed once they all have
([multi-doc.md](multi-doc.md#waiting-for-a-cut)).

## items.json

```json
{
  "version": 1,
  "doc_hash": "3f9c...",
  "doc_version": "2026-10-11T05:49:09.880Z",
  "last_sync": "2026-10-10T14:03:00+00:00",
  "items": {
    "login": {
      "id": "login", "kind": "task", "title": "Login form", "tag": "login",
      "parent": "auth", "order": 2, "level": 3, "line": 14,
      "markers": [], "inherited": ["next"], "checked": false,
      "body": "Done when: bad passwords show an error",
      "acceptance": ["bad passwords show an error"],
      "state": "awaiting-review", "branch": "ddd/login", "commits": ["9ab1..."],
      "edits": ["2026-10-08T09:00:00+00:00"], "flags": [], "aliases": [],
      "comments": [{"id": "AAA...", "kind": "status", "ts": "...", "state": "awaiting-review"}],
      "question": null, "notes": [{"ts": "...", "text": "..."}],
      "created": "...", "seen": "...", "built_at": "...", "done_at": null, "done_by": null
    }
  }
}
```

`doc_version` is the Google Doc's version (modified time or revision id) at
the last sync, recorded by `diff --doc-version` and `accept`.
FILE arguments (`diff`, `parse`) take Markdown, a `.b64` file, or a connector's
result saved as JSON (`{"content": "<base64 or text>"}`).

`needs`: the item's `Needs:` references. `moved_from`: qualified ids it had in
other docs; `old_trailers`: bare ids its commits carried before the project had
several docs. `flags`: `changed-after-build`, `removed-from-doc`. `done_by`: `developer` (ticked
in the doc), `merge`, `agent`. `derived_from` / `merged_into` record splits and
merges. `doc_hash` covers the doc without the status section, with whitespace
normalised.

## log.jsonl

One JSON object per line, each with `ts` and `type`:

| type | Fields |
|---|---|
| `init` | `doc` |
| `sync` | `doc_hash`, `prev_doc_hash`, `summary` |
| `doc-change` | `item`, `changes` (e.g. `["renamed","queued"]`, `["new"]`, `["removed"]`), `title`, `old_title`, `doc_hash` |
| `conflict` | `item`, `conflict` |
| `split` / `merge` | `item`/`items`, `into` |
| `state` | `item`, `from`, `to`, `branch`, `commits`, `note`, `question`, `tests`, `how` |
| `commit` | `item`, `commit`, `branch` |
| `status-write`, `config` | what was written or set |
| `move` | `item`, `from` (qualified id in the doc it left) |
| `doc-migrate`, `doc-add`, `doc-rename`, `doc-remove` | `doc`, and `source` / `from` |

With several docs every entry also has `doc`.

Together with the trailers in git, this answers "which doc change led to which
commits": `ddd log --item login`.

## Commands

| Command | Does |
|---|---|
| `init --doc PATH\|URL [--base B] [--test CMD] [--force]` | Create `.ddd/` |
| `info` | Config, state counts, flags, git branch and dirty files |
| `parse FILE [--outline] [--compare OTHER]` | Parse a doc and print its items, one line per item, or what differs in OTHER (no state needed; for validating drafts and conversions) |
| `survey [--limit N] [--scope DIR]` | Facts about the project for Initialize mode: stack, manifests, tests, CI, docs, code markers, git with `base_guess`/`on_base` (no state needed). `--scope`: one folder only, plus `scope` with its test command, git history and the tracked `related_items` about it (New doc mode) |
| `diff [FILE] [--doc-version V]` | Compare the doc with the last sync; saves a pending diff. Google Doc: without FILE, only reports whether V (modified time or revision) differs from the last synced version (`fetch_skipped` / `fetch_needed`) |
| `accept [--match K=ID] [--new K] [--split ID=K,K] [--merge ID,ID=K] [--accept-proposed]` | Apply the pending diff (several docs: every pending diff at once; keys as `DOC:KEY`; `--move DOC:KEY=DOC:ID`, `--no-move DOC:KEY`) |
| `rank [--queued] [--limit N]` | Work candidates with signals (`stack_on`, `needs_unresolved`), `blocked`, and `waiting` (items whose `Needs:` are not done); `--queued` for auto mode |
| `doc add NAME PATH\|URL [--scope DIR] [--test CMD] [--first-name NAME] [--wait-for ID]...` / `doc list` / `doc rename OLD NEW` / `doc remove NAME [--force]` | Several docs ([multi-doc.md](multi-doc.md)) |
| `doc suggest PATH\|URL [--file SAVED]` | Best-guess `doc add` settings for a doc not tracked yet: name, the existing doc's name, scope, test command, items already tracked elsewhere, and what to confirm. Changes nothing; works without `.ddd/` ([multi-doc.md](multi-doc.md#adding-a-doc-interactively)) |
| `items [--state S]` / `show ID` | List items / full record |
| `set ID [STATE] [--note] [--question] [--branch] [--commit SHA] [--comment-id ID --comment-kind K] [--clear-flags]` | Update an item |
| `start ID [--from BRANCH]` | Branch and mark in progress |
| `commit ID -m MSG [-m MSG]` | Commit staged changes with the trailer |
| `finish ID --tests-passed SUMMARY [--note] [--stay]` | Mark awaiting review, return to base |
| `checkout BRANCH` | Switch branches, keeping `.ddd/` |
| `sync-git` | Mark merged items done |
| `status-block [--write PATH] [--expect-hash H]` | Render or write the status section (Markdown doc) |
| `status-block --gdoc-read READ [--tab T]` / `--gdoc-check READ` | Google Doc: the guarded `batchUpdate` requests that replace the status section, built from a `read_doc` result (saved by the host, or trimmed as in backends.md); then check a fresh read against what was written (saved in `.ddd/tmp/gdoc-status.json`) |
| `commit-state [-m MSG]` | Commit `.ddd/` on the base branch |
| `config get [KEY]` / `config set KEY VALUE` | Dotted keys; VALUE parsed as JSON when possible |
| `log [--item ID] [--limit N]` | Audit trail |

With several docs, `--doc NAME` picks the doc for `diff`, `accept`,
`status-block`, `show`, `set`, `start`, `commit`, `finish` and `config`, and
filters `rank`, `items`, `info` and `log`. Item ids may be qualified (`doc:id`).

Exit codes: `0` ok, `1` error, `2` input needed, `3` doc changed since last sync.
