# Git workflow

## One branch per item

`ddd start ID` creates `ddd/ID` (prefix: `branch_prefix`; with several docs
`ddd/<doc>/ID`, and the trailer below is `DDD-Item: doc:ID`) from the base branch,
or checks out the existing branch to resume. It refuses (exit 2) while tracked
files have uncommitted changes, apart from `.ddd/` and a local Markdown doc.
Untracked files don't block. Ask the developer; never stash or discard their
work. To build on top of an unmerged item: `ddd start ID --from ddd/OTHER`.

Commits go through `ddd commit ID -m "..."`, which:

- requires you to be on the item's branch,
- commits only what you staged (stage files explicitly; don't `git add -A` build
  output or secrets),
- refuses if `.ddd/` is staged,
- appends the trailer `DDD-Item: ID`, which links the commit to the doc item in
  `git log` and lets merge detection survive squash merges.

`ddd finish ID --tests-passed "<what ran>"` records the commits in
`base..branch` that carry the item's trailer (a stacked branch also contains
its parent item's commits, which must not count), marks the item
`awaiting-review`, clears its conflict flags
and switches back to the base branch. Without `--tests-passed` it refuses. A
failing item becomes `blocked` with a note instead.

## Independent or stacked branches

Items branch from the base by default, so each can be reviewed and merged on
its own. When two queued items will edit the same code (two functions added at
the same spot, the same import line), sibling branches conflict on the second
merge. Then stack the later item: `ddd start B --from ddd/A`. Merging B brings A
along. Merging only A leaves B awaiting review, because each item tracks only
its own commits. Say in the report which items you stacked and why.

## Where the state lives

`.ddd/` is committed **only on the base branch**, by `ddd commit-state` at the
end of a run. Item branches never commit it, so state never conflicts when
branches merge.

While you switch branches, `.ddd/` and a local Markdown doc "float": `ddd
checkout`, `start` and `finish` back them up, reset them so git allows the
switch, and restore them afterwards. So the newest state, the status section
and the developer's uncommitted plan edits stay in the working tree, whichever
branch is checked out. Always switch branches with `ddd checkout <branch>`
rather than plain `git checkout` while a run is in progress.

The doc is never committed by ddd. The developer commits it when they like,
status section included. Item branches must not touch it either: `ddd commit`
commits only what you stage, so don't stage the doc.

`finish` lists `untracked` files on the item branch. One the item needs, left
uncommitted, means a broken branch: commit it and finish again.

## Review and done

| State | Meaning |
|---|---|
| `todo` | Not started |
| `in-progress` | Branch exists, being built |
| `awaiting-review` | Tests passed, waiting for the developer to review and merge |
| `done` | Merged into base, ticked in the doc, or confirmed with `ddd set ID done` |
| `blocked` | Waiting on an answer (`question`) or a problem (`note`) |
| `removed` | Gone from the doc (kept for history and in case it comes back) |

`ddd sync-git` marks an `awaiting-review` item done when one of its recorded
commits is an ancestor of the base branch (merge or fast-forward), or when a
base-branch commit carries its exact `DDD-Item: ID` trailer (squash merge;
GitHub keeps the commit messages in the squash body by default). Rebase merges
rewrite hashes; the trailer survives them too.

## What you never do unasked

Push, open pull requests, merge, rebase shared branches, delete branches, or
force anything. When the developer asks for a pull request, use the branch name
and the item's title and acceptance criteria for its description.

## Projects without git

Setup offers `git init`. If the developer declines, skip `start`, `commit`,
`finish`, `checkout` and `sync-git`. Build in place and track states with
`ddd set ID in-progress`, `ddd set ID awaiting-review --note "tests: ..."` and,
once the developer confirms, `ddd set ID done`.
