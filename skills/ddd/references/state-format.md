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

## items.json

```json
{
  "version": 1,
  "doc_hash": "3f9c...",
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

`flags`: `changed-after-build`, `removed-from-doc`. `done_by`: `developer` (ticked
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

Together with the trailers in git, this answers "which doc change led to which
commits": `ddd log --item login`.

## Commands

| Command | Does |
|---|---|
| `init --doc PATH\|URL [--base B] [--test CMD] [--force]` | Create `.ddd/` |
| `info` | Config, state counts, flags, git branch and dirty files |
| `parse FILE` | Parse a doc and print its items (no state needed; for debugging) |
| `diff [FILE]` | Compare the doc with the last sync; saves a pending diff |
| `accept [--match K=ID] [--new K] [--split ID=K,K] [--merge ID,ID=K] [--accept-proposed]` | Apply the pending diff |
| `rank [--queued] [--limit N]` | Work candidates with signals; `--queued` for auto mode |
| `items [--state S]` / `show ID` | List items / full record |
| `set ID [STATE] [--note] [--question] [--branch] [--commit SHA] [--comment-id ID --comment-kind K] [--clear-flags]` | Update an item |
| `start ID [--from BRANCH]` | Branch and mark in progress |
| `commit ID -m MSG [-m MSG]` | Commit staged changes with the trailer |
| `finish ID --tests-passed SUMMARY [--note] [--stay]` | Mark awaiting review, return to base |
| `checkout BRANCH` | Switch branches, keeping `.ddd/` |
| `sync-git` | Mark merged items done |
| `status-block [--write PATH] [--expect-hash H]` | Render or write the status section |
| `commit-state [-m MSG]` | Commit `.ddd/` on the base branch |
| `config get [KEY]` / `config set KEY VALUE` | Dotted keys; VALUE parsed as JSON when possible |
| `log [--item ID] [--limit N]` | Audit trail |

Exit codes: `0` ok, `1` error, `2` input needed, `3` doc changed since last sync.
