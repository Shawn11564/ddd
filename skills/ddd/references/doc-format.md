# Planning doc format

The doc is free-form. The agent reads whatever you write. A few optional
conventions make it predictable. [templates/plan.md](../templates/plan.md) is a
starting point.

## Items

| You write | The agent sees |
|---|---|
| `## Heading` (any level) | A feature. Nested headings nest. |
| `- [ ] text` or `1. [ ] text` | A task under the nearest heading, or under a less-indented task |
| `- [x] text` | A task you have marked done |
| `☐ text` / `☑ text` | The same, as Google Docs exports checklists |
| Other lines | The description of the item above them. Indented lines under a task belong to that task. |
| `Done when: ...` (also `Acceptance:`) | The item's acceptance criteria; the agent writes tests for them |
| `Needs: {#tag}` (also `{#doc:tag}`, several allowed) | The item waits until those items are done. A `Needs` line without a `{#...}` reference is plain description |

A heading with tasks under it is a group; the agent builds its tasks. A heading
with no tasks but a description is built as one item. A heading whose tasks are
all done counts as complete. Code blocks are description text, never items.

## Markers

Put markers anywhere on an item's line. They apply to everything nested under it.

| Marker | Meaning |
|---|---|
| `[next]` | Queued: `ddd auto` builds these, in doc order |
| `[blocked]` | Off limits until you remove the marker |
| `[notes]` | Context only, never work (background, ideas, decisions) |
| `{#tag}` | Pins identity: the agent tracks the item by this tag however you reword or move it. Letters, digits, `-` and `_`. The tag also names the branch (`ddd/tag`). |

Tags are optional. Untagged items are matched by their text, their parent and
their position (see [matching.md](matching.md)). Tag the items you expect to
rework heavily. Adding a tag later is safe: the agent adopts it as the item's id.

A heading's tag and markers can also go on their own line directly under it:

```markdown
## Finish Clash

{#clash} [next]

Why it matters...
```

Use this form in Google Docs. Google cuts a heading at its first `{` when it
imports Markdown, losing the tag and everything after it, but keeps a tag line
(it exports it escaped, `{\#clash} \[next\]`, which the agent reads the same).
The line must be the first one after the heading, holding nothing but tags and
markers; anywhere else it is ordinary description text.

## What you can do freely

- Reword, reorder, move items between sections, change heading levels, turn a
  heading into a task or back. The agent follows.
- Delete items. The agent notices. If it was building one, it asks.
- Split or merge items. The agent proposes what it thinks happened and asks.
- Tick a box. The agent marks the item done. Untick it to reopen.
- Edit an item after the agent built it. It flags the change and asks whether
  to rebuild.

## The status section

The agent maintains one section, headed `DDD Status` (configurable as
`status_section`), at the end of the doc by default. It holds in-progress,
awaiting-review, blocked items (with questions) and recently finished work.
Anything you type inside it is overwritten. The agent writes nowhere else in
the doc, apart from comments on Google Docs. The section never counts as a doc
change and never produces items.

To answer an agent question, reply to its comment (Google Docs), answer in
chat, or edit the item in the doc and remove `[blocked]`.
