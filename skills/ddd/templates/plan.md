# Project plan

Write this doc however you like. The agent reads it on every run, works out
what changed since last time, and picks work from it. A few optional markers
make it more predictable:

- A heading is a feature. A checkbox under it is a task.
- `[next]` queues an item (or every open task under a heading) for `ddd auto`.
- `[blocked]` keeps the agent away from an item until you remove it.
- `[notes]` marks a section as context only, never work.
- `{#short-tag}` pins an item's identity, so the agent keeps tracking it
  however much you reword or move it. Optional; untagged items are matched by
  their text and position. In a Google Doc, put a heading's tag (and markers)
  on its own line right under the heading: Google drops a `{` in a heading.
- `Done when:` lines become the item's acceptance criteria.
- Check a box yourself to tell the agent an item is done.

Leave the "DDD Status" section at the end to the agent; it rewrites it.

## Background [notes]

What the project is, who it is for, constraints worth knowing.

## Sign-in {#auth} [next]

Users sign in with email and password.

- [ ] Login form with email and password fields {#login-form}
  - Done when: invalid credentials show an inline error
- [ ] Remember-me option that keeps the session for 30 days
- [ ] Password reset by email

## Export reports

Done when: a signed-in user can download the monthly report as CSV.

## Dark mode [blocked]

Waiting on the design tokens from the design team.

## Ideas [notes]

- Maybe a CLI later.
