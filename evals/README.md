# Evals

## matching/: identity tracking (automated)

Each folder is one reformatting scenario:

- `before.md`: the doc as last synced
- `after.md`: the doc after the developer's edit
- `expect.json`: what `ddd diff` must conclude

`_base.md` is the shared starting doc. `tests/test_ddd.py` (`MatchingEvals`)
runs every folder. Labels are an item's title, or `Parent > Title` when the
title alone is ambiguous.

| Key | Meaning |
|---|---|
| `matched` | `{label: id}`: must match this id confidently (no question asked) |
| `changes` | `{label: [types]}`: these change types must be reported |
| `new` / `removed` | Exact lists of new titles / removed ids |
| `ask` | `[{label, id}]`: the agent must ask about this pair (in `proposed` or `possible`). `[]` means no questions at all. |
| `splits` | `[{from, label}]`: must be flagged as split off |
| `no_hint_for` | Ids that must not appear in `possible` |
| `no_changes` / `same_hash` | Nothing changed / the doc hash is identical |
| `after_accept` | After accepting: `ids` `{label: id}`, `aliases` `{id: [...]}`, `states` `{label: state}` |

When a matching bug is reported, add a scenario that reproduces it first, then
fix the matcher until the scenario passes.

## skill/: agent behaviour (manual or harness-driven)

- `evals.json`: prompts with expectations, run against a fresh copy of
  `fixtures/tiny-app` (`git init`, commit everything, install the skill).
  Grade each transcript and the resulting repo against `expectations`. The
  format follows skill-creator's `evals.json`, so it can drive that tool's
  benchmark loop.
- `trigger-queries.json`: should and should-not trigger queries for tuning the
  `description` in `SKILL.md`.

Run the behaviour evals before each release, and whenever `SKILL.md` changes
meaningfully.
