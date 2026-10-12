# Item identity and resolving changes

`ddd diff` matches every item in the current doc against the items tracked so
far. Matching is what lets the developer reformat freely, and it is the part
most worth getting right. When unsure, the script asks; it never guesses.

## How items are matched

In order; each tracked item matches at most once:

1. **Confirmed**: matches the developer confirmed (`accept --match`).
2. **Tag**: `{#tag}` equals a tracked id or one of its aliases.
3. **Identical text**: same kind, title and description. Identical duplicates
   (two "Write tests" tasks) go to the one under the same parent.
4. **Similarity**, scored 0 to 1 from title similarity, description word
   overlap, matching parent and relative position. A heading-to-task change
   costs 15%; a previously removed item costs 10%.
   - At or above **0.78**: matched confidently.
   - **0.55 to 0.78**: `proposed`, which needs confirmation.
   - Below that: new item. The tracked item it might have been is listed under
     `possible` when it scores at least **0.35**.

New untagged items get an id slugged from their title (`password-reset-by-email`).
The id never changes on rename, so branches and commits stay linked. Adding a
`{#tag}` later retags the item: the tag becomes the id and the old id an alias.

## Reading the diff report

| Field | Meaning |
|---|---|
| `new` | Items not seen before (`key` like `n7` is their position in this diff) |
| `changed` | Matched items whose `types` include `renamed`, `edited`, `moved`, `queued`/`unqueued`, `blocked`/`unblocked`, `checked`/`unchecked`, `restored`, `retagged` |
| `removed` | Tracked items no longer in the doc |
| `proposed` | Uncertain matches: `key` is probably `id` (`score`) |
| `possible` | A removed `id` resembles doc item `key`: `kind` is `rename` (the doc item is unmatched) or `merge` (the doc item matched something else and its text changed) |
| `splits` | New `key` is mostly made of text from `from_id`: probably split off |
| `conflicts` | `changed-after-build`, `checked-while-active`, `removed-while-active` |
| `warnings` | Duplicate tags etc. |
| `needs_input` | Any of `proposed`, `possible`, `splits`, `conflicts` is non-empty |

## Asking the developer

Batch every question into one message, give a recommendation for each, and
accept short answers. For example:

> The doc changed in ways I want to check before I update my tracking:
> 1. "Night theme" looks like the old "Dark mode" (same description). Same item? *(I think yes.)*
> 2. "Keep users signed in for 30 days" may be a rewrite of "Remember-me option". Same item? *(Probably.)*
> 3. "Export reports as PDF" looks split off from "Export reports". Track it as a new item derived from it? *(Yes.)*
> 4. You edited "Login form" after I built it (branch `ddd/login`, awaiting review). Rebuild it to match, or keep it as built?

Then translate the answers:

| Answer | Flag |
|---|---|
| Yes, `key` is `id` | `--match KEY=ID` |
| No, it's new | `--new KEY` |
| A removed item was renamed to a new one | `--match KEY=ID` |
| `id` split into these | `--split ID=KEY1,KEY2` (the keys become new items with `derived_from`) |
| These were merged into one | `--merge ID1,ID2=KEY` (`KEY` keeps `ID1`'s history; the others are marked `merged_into`) |
| "All your guesses are right" | `--accept-proposed` |

`accept` re-runs matching with these constraints and refuses (exit 2) while any
`proposed` match is unanswered. Conflicts don't block `accept`. They become
flags on the item; act on the developer's answer afterwards with `ddd set`
(see SKILL.md, sync step 5).

## Moves between docs

With several docs ([multi-doc.md](multi-doc.md)), a new item in one doc is
also matched against items leaving the other docs: removed in their pending
diff, or removed in an earlier sync. Same tag or identical text is a sure
move (`joint.moves`); a similar item, or a tag still live in the other doc, is
a question (`joint.proposed_moves`): "Did 'X' move here from network's 'Y'?"
Yes → `--move DOC:KEY=DOC:ID`, no → `--no-move DOC:KEY`. A moved item keeps
its id (or takes its new tag), state, branch, commits and log, and records
`moved_from`.

## Hot items

Every change of type `renamed`, `edited`, `restored` or `queued` adds a
timestamp to the item's `edits`. The hot score sums `0.5 ^ (age_days /
hot_half_life_days)` (default half-life 7 days) over those timestamps, so items
the developer keeps returning to rank higher in menu mode. Changes are seen only
at sync time: several edits between two runs count once.
