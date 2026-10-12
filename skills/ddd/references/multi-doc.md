# Several docs in one repo

A repo can be planned in several docs: a network doc for the shared work and
one doc per game, service or package. One `.ddd/` at the repo root tracks them
all, so there is one base branch, one `commit-state`, one dirty-tree check that
knows every doc, and one menu across all of them. Each doc's status section
lists only that doc's items.

`ddd info` shows `docs` (a list) when the project has several; otherwise it has
one doc and nothing here applies.

## Adding a doc

```
ddd doc add NAME PATH|URL [--scope DIR] [--test "CMD"] [--first-name NAME]
```

- The first `doc add` in a single-doc project also names the existing doc:
  `--first-name network`. Ask the developer for both names. Its items keep
  their ids, recorded branches and history, and commits made before keep
  counting (bare `DDD-Item:` trailers stay recognised for that doc).
- Names are short slugs starting with a letter (`clash`, `orbit`, `network`),
  never a mode word (`auto`, `menu`, `status`, ...). `doc add` refuses a name
  whose branch `ddd/<name>` already exists.
- `--scope` is the folder the doc plans, e.g. `games/clash`. It only picks the
  default doc when ddd runs inside that folder; items may change code anywhere.
- `--test` sets the doc's own test command (e.g. `./gradlew :games:clash:test`);
  without it the repo's applies. Per-doc settings: `ddd config set
  test_command "..." --doc clash` (also `branch_prefix`, `status_section`,
  `doc.scope`, `doc.tab`).
- `ddd doc list`, `ddd doc rename OLD NEW` (old trailers still count; branches
  keep their prefix), `ddd doc remove NAME` (refused while items are in
  progress or awaiting review, unless `--force`).

Then sync: the new doc's first diff matches items that moved into it.

## Adding a doc interactively

`ddd doc add <path-or-url>` (Claude Code: `/ddd:doc add <url>`) gives you only
the doc. Work out the rest yourself, then confirm it with the developer in one
message. `ddd doc add NAME <path-or-url>` or flags (`--scope`, `--test`,
`--first-name`) settle those questions in advance.

1. **Read the doc once.** Markdown: nothing to do. Google Doc: get its
   modified time from the MCP's metadata tool, then fetch it (Markdown export
   preferred) and save it **outside the repo** (the host's temp or scratch
   directory). Don't save it in `.ddd/tmp/`, which the first `doc add` moves. If
   the host saved the result to a file, use that file. Keep the file and the
   modified time: the first sync reuses both, so the doc is never fetched twice.
2. **`ddd doc suggest <path-or-url> [--file <saved>]`.** It changes nothing
   and prints its best guess:
   - `already_tracked`: if this is not `false`, a doc already tracks this
     source (`null` means the only doc). Say so and stop.
   - `project`: `"none"` means there is no `.ddd/` yet, so this is
     [Setup](../SKILL.md#setup) with this doc. Use its `test_command`, skip
     the name questions, and start the first sync from the saved file.
   - `name.suggested` (and `alternatives`), already checked against reserved
     words, other docs and existing `ddd/<name>` branches. `first_name` names
     the existing doc in a single-doc project (from its title).
   - `scope.suggested` and `candidates` with reasons: folders named in the
     doc, matching its title, holding it, or having their own build file.
     `null` means the doc plans the whole repo.
   - `test_command.suggested` (with `why`) for the code in that scope, or
     `null` to keep the repo's (`repo`). `verified` is always false.
   - `overlap`: items already tracked in another doc (`how`: tag, exact,
     similar; `doc: null` is the existing doc). The `live` ones are still in
     that doc (`checked`: the Markdown file as it is now, or a Google Doc's
     last sync).
   - `outline`, `warnings`, `markers`, `needs`, and `confirm`: the questions
     still open.
3. **Check the guesses.** Look at the scope candidates in the tree. Then run
   the suggested test command (or the repo's, scoped if you can), so you can
   say whether it passes. A failing or very slow run is worth saying, not a
   reason to guess another command. Settle what the code can tell you
   yourself; ask only what it can't.
4. **Ask once**, in one message, with your recommendation for each answer,
   so "yes" accepts them all:
   - the doc's name, and in a single-doc project the existing doc's name;
   - the scope (or none);
   - the test command, as it ran and with its result (or "use the repo's");
   - if any `overlap` items are `live`, list them (the first few and a
     count). If the section moved here, the developer cuts it from the other
     doc before you sync, and the items keep their state and branches.
     If it's a copy, they become separate items, and a tag in both docs
     comes back as a move question at the sync (`--no-move`);
   - parse `warnings` and any `needs` reference that names no tracked item.
   Mention what conventions the doc uses (`markers`, tags) only if it
   uses none; don't ask about them.
5. **Add it:** `ddd doc add NAME <path-or-url> [--first-name NAME] [--scope DIR]
   [--test "<cmd as it ran>"]`. Leave out `--test` if no command ran. Heed
   any `warnings`. If the developer says the items moved but hasn't cut them
   from the other doc yet, add `--wait-for ID` for each live one (see
   [Waiting for a cut](#waiting-for-a-cut)), and with a Google Doc also
   `--file <saved file> --doc-version <modified time>` from step 1, so the
   sync after the cut doesn't fetch it again.
6. **Sync every doc** ([Syncing several docs](#syncing-several-docs)). For
   the new doc, run `ddd diff --doc NAME <saved file> --doc-version <modified
   time>` with the file and time from step 1. Moves need no question;
   `proposed_moves` do. That second question can only come after the sync.
   Then one `ddd accept`.
7. Google Doc: record its capabilities if config has none yet, or re-check
   them if this session offers a tool they lack
   ([backends.md](backends.md#google-docs-through-an-mcp)). Then write its status section, run
   `commit-state`, and report: the name, scope and test command, the item
   count, what moved in with its state, and how to work on it alone
   (`ddd auto NAME`, `ddd status NAME`).

`ddd doc list`, `rename` and `remove` need no analysis: run them. Before
`remove`, confirm with the developer, because it deletes that doc's state.

## A new doc for part of the project

`ddd doc new <folder> [markdown|gdoc] [name]` (Claude Code: `/ddd:doc new
games/clash`) drafts a doc of its own for one game, service or package from
its code, then adds it. It is [Initialize](initialize.md) limited to one folder,
plus carrying over what the existing docs already plan for that folder.

1. **Sync first.** Run the [usual sync](../SKILL.md#every-run-starts-with-a-sync)
   on every doc before anything else, and settle its questions. The items
   you carry over must be the docs' current text, and `related_items` comes
   from the last sync. A Google Doc that changed is fetched now, once: don't
   sync it again after `doc add`, since it is read again after the cut.
2. **Find the folder.** The arguments name a folder (`games/clash`) or a part
   of the project ("the clash game"): find its folder in the tree. If the
   part has no folder of its own, survey without `--scope`, read only its
   code, and add the doc without `--scope`.
3. **`ddd survey --scope <folder>`.** The usual survey fields (files,
   manifests, tests, docs, code markers) for that folder only, plus `scope`:
   - `test_command`: the folder's own test command (Gradle subproject, npm,
     Maven, Cargo, Go, pytest, make), or `null` to keep the repo's.
   - `git`: commits touching the folder, recent subjects, and unmerged
     branches with work in it.
   - `related_items`: tracked items about the folder, in every doc, in doc
     order. `why` is `path` (the item names the folder's path), `title` (its
     title or tag names the folder), `body` (its text names the folder), or
     `in section` (under a heading whose title or tag names it). A passing
     mention in a section's text lists that heading alone, not everything
     under it, and the doc's title heading is never listed. The `title`
     sections are the ones the new doc takes over; judge the rest one by one.
   Then read the folder's README, manifests, entry points and tests (in a
   subagent if it is large), as in [initialize.md](initialize.md#1-survey).
   Run the test command if it is cheap.
4. **Ask once**, with a recommendation for each:
   - the format: a Markdown file (default `<folder>/plan.md`) or a Google Doc
     (title `<Name> plan`), unless the arguments say;
   - the doc's name (default: the folder's name, checked as in
     `doc suggest`), and in a single-doc project the existing doc's name
     (from its title);
   - the test command as it ran and with its result, or the repo's;
   - the `related_items`, grouped by section: do they move to the new doc
     (recommended for sections about this part only), or stay where they
     are? If they move, the developer cuts them from the other doc after the
     new one exists. Items about shared work stay put.
   No `.ddd/` yet: the doc is the project's first. Skip the names and
   related items, and finish with `ddd init` instead of `doc add`.
5. **Draft** with the structure in [initialize.md](initialize.md#3-draft),
   for this folder only: overview, current features, known issues, planned
   features, open questions. Then:
   - **Carry moving items over word for word**: title, body lines,
     `Done when:`, checkbox and markers. Copy them from the doc itself
     (Markdown) or from its snapshot (`.ddd/snapshot.md`, or
     `.ddd/docs/<name>/snapshot.md` with several docs), which step 1 just
     brought up to date, so a Google Doc needs no second fetch. Give each one its
     current id as a tag (`{#clash-kits}`, `{#step-1-core-extraction}`; keep a
     tag it already has), so it moves even if the text was edited since.
     Never tick a box that isn't ticked there.
   - Work the folder shares with the rest of the repo stays in the other
     doc. Link it with `Needs: {#network:live-reload}` instead of copying it.
   - New items come from the code, with evidence, as in Initialize. No
     `[next]`, and no status section.
6. **Validate.** Save the draft outside the repo. `ddd parse <draft>
   --outline` should report no warnings. Then `ddd doc suggest <draft-path>
   --file <draft>`: every carried item should be in `overlap` with
   `how: tag` and the id you meant. Anything else in `overlap` was copied by
   accident.
7. **Write** as Initialize does: a Markdown file (ask before overwriting
   one; leave it uncommitted), or a Google Doc created **once** from the
   Markdown, read back and checked with `ddd parse <draft> --compare <read-back>`.
   Google Doc: get its `modifiedTime` from the metadata tool just before the
   read-back and again after it. If the two match, that is the read-back's
   version.
8. **Add it:** `ddd doc add NAME <path-or-url> --scope <folder> [--first-name
   NAME] [--test "<cmd as it ran>"] --wait-for ID ...`, with one `--wait-for`
   for each carried item that is still in the other doc. Google Doc, when the
   two times matched: add `--file <read-back> --doc-version <modifiedTime>`.
   The read-back is kept for the first sync after the cut, which then skips
   fetching the new doc if it is still at that version (`saved_fetch_used`).
   Then tell the developer exactly what to cut: the doc, and the headings
   with their lines. Don't sync again: step 1 did, and the new doc would
   only report `held`. If they cut it during the run and say so, sync every
   doc: the items move with their state, branches and history. Then
   `commit-state`, and report the doc's path or link, its item count, what
   moved (or still waits) and what you would queue first.

## Waiting for a cut

An item moves between docs only if one sync sees it leave the old doc and
appear in the new one. If the new doc is synced first while the item is still
in the old doc, it becomes a second, separate item. `doc add --wait-for ID`
(repeatable) prevents this. The new doc's `diff` then reports `held: true`
with `waiting_for` (the items still in their doc) and records nothing, until
every one has left: cut from a Markdown file, removed in this run's pending
diff of the other doc, or already synced as removed. Then the hold lifts by
itself, and that same sync reports the moves. A read-back saved with `doc
add --file --doc-version` survives the hold; `ddd info` shows it as the doc's
`saved_fetch`. A diff with a newer `--doc-version` drops it as stale.

`ddd info` lists a doc's `wait_for`. If the developer decides to keep both
copies after all: `ddd config set doc.wait_for "[]" --doc NAME`.

## Ids, branches and trailers

- Item ids belong to their doc. Outside it, write `doc:id` (`clash:kits`); a
  bare id works when only one doc has it. `rank`, `items`, `start` and
  `finish` print qualified ids: pass them on as printed.
- Branches are `ddd/<doc>/<id>` (`ddd/clash/kits`); an item keeps the branch
  it was started on.
- Commits carry `DDD-Item: clash:kits`, so merge detection never mixes up two
  docs that both have `{#lobby}`.

## Which doc a command uses

`--doc NAME`, else the only doc, else the doc whose scope holds the working
directory. Otherwise a command that works on one doc (`diff`, `status-block`)
exits 2 listing the docs: pass `--doc`. Reporting commands (`rank`, `items`,
`info`, `log`) cover every doc unless given `--doc`; `sync-git` and
`commit-state` always cover all.

## Syncing several docs

Every run syncs every doc, then accepts once:

1. Per doc, get its text and run `ddd diff --doc NAME [file] [--doc-version V]`.
   Google Docs: check each doc's modified time first; only changed docs are
   fetched, so a game edit no longer re-fetches the whole plan.
2. Read the **last** diff's `joint`: `moves` (items that moved between docs,
   matched by tag or identical text; no question needed) and `proposed_moves`
   (a similar item, or a tag that is still live in the other doc). Ask about
   each proposed move in the same message as the other questions: "Did
   'Step 4: roles, kits and colours' in clash move from network's 'Step 4:
   roles and kits'?"
3. One `ddd accept` applies every pending diff. Answers: `--move
   clash:n4=network:clash-kits` (yes), `--no-move clash:n4` (a new item);
   matches and splits take `DOC:KEY` too (`--match clash:n3=kits`).

A moved item keeps its id, state, branch, commits and log; `moved_from` records
where it came from, and `show network:old-id` still finds it. Each doc's diff
already leaves moved items out of `new` and `removed`, so a section cut from
one doc and pasted into another raises no question.

## Needs

A body line `Needs: {#tag}` or `Needs: {#doc:tag}` (several references allowed)
says the item can't start until those items are done. A Needs line without a
`{#...}` reference is plain prose.

- `rank` leaves a waiting item out of the candidates and lists it under
  `waiting` with what it needs. Auto mode skips it; the status section lists a
  queued waiting item under **Waiting**.
- If the only open need is awaiting review, the item is a candidate with
  `signals.stack_on`: build it with `ddd start ID --from <that branch>` and say
  so (it merges after its need).
- A reference that matches nothing is a `warnings` entry in the diff: tell the
  developer once.

## Modes

- `ddd menu` / `ddd status`: every doc; label each option with its doc.
- `ddd auto`: queued items of every doc, docs in `doc list` order. `ddd auto
  clash` or `ddd status clash`: one doc (`rank --queued --doc clash`).
- Write-back: one status section per doc (`status-block --doc NAME ...`), each
  listing that doc's items only. `commit-state` once at the end.
