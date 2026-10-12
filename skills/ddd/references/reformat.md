# Reformatting a doc

`ddd reformat [doc] [full|less]` (Claude Code: `/ddd:reformat`) rewrites a
planning doc in another style. It changes the doc's look, never its plan.

- **full**: the template's layout ([templates/plan.md](../templates/plan.md),
  [initialize.md](initialize.md#3-draft)). Every work item is tagged with its id,
  so the developer can reword anything afterwards and the agent still follows.
- **less**: as light as the conventions allow, as if for a first draft. It is
  for a developer getting used to ddd who wants a doc that reads like ordinary
  notes.

## What a reformat keeps

Both styles keep these exactly. `ddd reformat check` enforces it:

- **Every item.** Every heading and every task, with no new work. The only
  thing you may add is a `[notes]` section (context, never work).
- **Its words.** The words of each title and description line. They are
  compared without case and punctuation, so bold, code spans, list markers,
  `**Label:**` prefixes and line breaks are yours to change. Words are not:
  no rewording, summarising or fixing typos.
- **Its section and order.** Each item stays under the same parent. Work
  items keep their order, because doc order is build order. `[notes]`
  sections may move.
- **Markers and checkboxes.** `[next]`, `[blocked]` and `[notes]` stay on the
  same items, ticked boxes stay ticked and open ones stay open. A task needs
  its checkbox in both styles: without one, it becomes description text.
- **`Done when:` and `Needs:` lines**, under the same item.
- **The status section.** Leave it out of the draft. `reformat write` keeps the
  doc's current one, and a Google Doc gets it at the next write-back.

Tags may change: adding a tag with the item's own id is always safe. Dropping a
tag is safe when the item's words still match without it, and the check says
so.

## Full

```markdown
# Shop plan

An online shop for prints. Checkout works; accounts don't exist yet.

## Overview

[notes]

- **Stack:** Django 5, Postgres.
- **Test:** `pytest`

## Sign-in

{#auth} [next]

Users sign in with email and password.

- [ ] Login form with email and password fields {#login-form}
  - Done when: invalid credentials show an inline error
- [ ] Remember-me option {#remember-me}
  - Needs: {#login-form}

## Dark mode

{#dark-mode} [blocked]

Waiting on the design tokens.
```

- `#` for the title, `##` for top-level features, `###` for features under
  them. Fix skipped levels only where that keeps every parent the same.
- Each work heading and task carries its id as a tag. Use the id that `ddd
  items` shows, never a new one, and keep any existing tag. A heading's
  tag and markers go on their own line right under it (as Google Docs needs).
  A task's tag and markers go at the end of its line. `[notes]` sections get
  a `[notes]` line and no tag.
- Tasks are `- [ ]` and `- [x]` (not `1. [ ]` or `☐`). `Done when:` and
  `Needs:` are indented sub-bullets under their task. Under a heading
  they sit on their own line after its description.
- Description: the developer's own sentences, in short paragraphs. Facts in
  notes sections can become `**Label:** text` bullets.
- If the doc has no guide yet, you may add the template's short "how this
  doc works" bullets as a `[notes]` section after the title.

## Less

```markdown
# Shop plan

An online shop for prints. Checkout works; accounts don't exist yet.

## Overview [notes]

Django 5 and Postgres, tests with pytest.

## Sign-in [next]

Users sign in with email and password.

- [ ] Login form with email and password fields
  Done when: invalid credentials show an inline error
- [ ] Remember-me option
  Needs: {#login-form}

## Dark mode [blocked]

Waiting on the design tokens.
```

- Plain headings and sentences. No bold labels, no tag lines, no sub-bullets
  for `Done when:`. Keep only the markers the doc already has, at the end of
  the heading or task line.
- Keep a tag only where it earns its place:
  - items in progress, awaiting review or blocked;
  - items that a `Needs:` line points to, in this doc or another;
  - items `reformat check` reports as `uncertain` without it.
  Drop the rest. The id stays in `.ddd/`, and the words carry the match.
- **Google Doc:** a heading that keeps a tag still needs its tag line under
  it, because Google cuts a heading at its first `{`. Markers alone can stay
  in the heading.

## Steps

1. **Sync first** ([the usual sync](../SKILL.md#every-run-starts-with-a-sync)),
   and settle its questions. The reformat starts from the doc as last
   synced, and the script refuses an open diff or a Markdown file edited
   since (exit 3).
2. **Pick the doc and the style**, and ask once for anything the arguments
   leave out:
   - **Which doc:** in a project with one doc, that doc. With several docs and
     none named, the script exits 2 with the list of `docs`; ask which one.
   - **Which style:** recommend the one the doc is not in now.
   - **How to write a Google Doc:** a new doc (recommended), or in place by
     the developer (see step 5).
3. **Draft** from the doc's current text: the Markdown file, or for a Google
   Doc `.ddd/snapshot.md` (`.ddd/docs/<name>/snapshot.md` with several docs),
   which the sync just updated. Get the ids from `ddd items`. Save the draft
   outside the repo. Don't put a status section in it.
4. **Check** with `ddd reformat check <draft> [--doc NAME]` until `ok` is
   true. Each problem says how to fix it:
   - `lost`: an item went missing, or lost its checkbox;
   - `new`: work was added;
   - `uncertain`: the match needs a tag with the item's id;
   - `changed`: its words, section, markers or checkbox changed;
   - `reordered`: work items are out of order.
   `warnings` are parse warnings and count as problems too. `tagged` counts
   the items pinned by their own id, and `added_notes` lists the `[notes]`
   sections you added.
5. **Write.**
   - **Markdown:** `ddd reformat write <draft> [--doc NAME]`. It runs the check
     again and refuses on any problem. It keeps the current status section,
     and saves the old text to `.ddd/tmp/before-reformat.md`
     (`.ddd/docs/<name>/tmp/` with several docs). Then sync: the diff shows
     only retagged items and any added notes, with no questions. Accept it
     and write back.
   - **Google Doc, new doc:** `ddd.py` can't replace a Google Doc's content,
     and you never rewrite a whole doc in place. So:
     1. Create a new doc from the draft, once, as
        [Initialize](initialize.md) does (title: the old title).
     2. Read it back, then run `ddd parse <draft> --compare <read-back>` and
        `ddd reformat check <read-back>`.
     3. Get the old doc's `modifiedTime` and run `ddd reformat relink
        <new-url> --old-version <it> [--doc NAME]`. It refuses if the old
        doc changed since the sync: sync it, then redo the draft.
     4. Run `ddd diff <read-back> --doc-version <new modifiedTime>` and
        accept.
     Tell the developer the old doc is untouched (its comments and history
     stay there), and give the new link. Never trash the old doc.
   - **Google Doc, in place, by the developer:** save the draft to `.ddd/tmp/`
     and tell them how to swap it in. In Google Docs, enable Markdown
     (Tools → Preferences), select everything above the status section, and
     use Edit → Paste from Markdown. The round-trip fixtures don't cover this
     path. After they say it is done, sync, run `ddd reformat check` on the
     fetched text before accepting, and report anything it finds.
6. **Report** in a few lines: the doc and style, how many items were kept
   (`kept` of `tracked`), the tags added or dropped, any notes sections
   added, and where the old version is (the backup, or the old doc's link).
