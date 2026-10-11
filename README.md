# ddd: Document-Driven Development for AI coding agents

You plan in a doc. Your agent builds from it.

`ddd` is an [Agent Skill](https://agentskills.io) for Claude Code, Codex and
other hosts that read `SKILL.md`. You keep a planning doc, a Google Doc or a
Markdown file, and rewrite and reformat it whenever you like. On each run the
agent:

1. **reads the doc and works out what changed** since last time (new, edited,
   moved, removed, queued, ticked), even after heavy reformatting;
2. **reconciles that with what it built**, asking you only when it is genuinely
   unsure ("Is 'Night theme' the old 'Dark mode'?");
3. **offers a ranked menu** of what to build next, or **builds your `[next]`
   items on its own**, one git branch per item, tests included;
4. **reports back** in a status section it owns at the end of your doc, and in
   doc comments where the Google Docs connector supports them.

Your doc stays yours: the agent never edits your text. It keeps its own state,
history and audit trail in `.ddd/` in your repo.

> "DDD" here is not Domain-Driven Design.

## Install

**Claude Code: plugin marketplace** (versioned updates)

```
/plugin marketplace add Shawn11564/ddd
/plugin install ddd@ddd
```

Plugin skills are namespaced: invoke it as `/ddd:ddd` (`/ddd:ddd auto`, ...),
or use the per-mode commands `/ddd:auto`, `/ddd:status`, ... (see [Use](#use)).

**Claude Code or Codex: copy the skill** (invoked as `/ddd`, or ask for "ddd")

```sh
git clone https://github.com/Shawn11564/ddd && cd ddd
./install.sh              # macOS/Linux/Git Bash: Claude Code and/or Codex, whichever you have
./install.ps1             # Windows PowerShell
./install.sh --target project   # into the current repo's .claude/skills and .agents/skills
```

Or copy `skills/ddd/` by hand into `~/.claude/skills/` (Claude Code) or
`~/.agents/skills/` (Codex).

**Requirements:** Python 3.8+ (standard library only) and git. For Google Docs,
a Google Drive or Workspace MCP connected to your agent; no Google credentials
are needed by the skill itself.

## Use

| You say | What happens |
|---|---|
| `/ddd` or "what should I work on next?" | Sync, then a ranked menu with reasons; builds what you pick |
| `/ddd auto` (or `/ddd auto 3`) | Sync, then builds `[next]` items in doc order until the queue is empty, it needs your input, or N items are done |
| `/ddd status` | Sync and report; builds nothing |
| `/ddd initialize` | Surveys the code and drafts the first plan for you, as a Markdown file or a Google Doc (you choose), then sets up |
| `/ddd setup` | First-run setup (also automatic) |

**Completion in Claude Code:** every mode is also its own command, so type
`/ddd:` and the menu lists them all: `/ddd:menu`, `/ddd:auto [max-items]`,
`/ddd:status`, `/ddd:setup [doc-path-or-url]`, `/ddd:initialize [markdown|gdoc]`.
The plugin provides them; `install.sh`/`install.ps1` copy them to
`~/.claude/commands/ddd/` (or `.claude/commands/ddd/` with `--target project`).
Use one install method, not both, or each command is listed twice.

On the first run it asks for your doc (a repo path or a Google Docs URL). No
plan yet? `/ddd initialize` reviews the project (README, manifests, entry
points, tests, CI, `TODO`/`FIXME`s, git history) and writes one: current
features as context, known issues, planned features with tasks, `Done when:`
lines and tags, and open questions. It never queues work for you; it suggests
what to mark `[next]`. Or start from [the blank template](skills/ddd/templates/plan.md).

### Writing the doc

Write freely. Optional conventions ([full reference](skills/ddd/references/doc-format.md)):

```markdown
## Sign-in {#auth} [next]          <- feature; {#tag} pins identity; [next] queues it

Users sign in with email and password.

- [ ] Login form {#login}          <- task
  - Done when: bad passwords show an inline error   <- acceptance criteria -> tests
- [ ] Password reset by email
- [x] Logout button                <- you ticked it: done

## Dark mode [blocked]             <- off limits until you remove [blocked]
## Background [notes]              <- context only, never work
```

Reword, reorder, move, split, merge, delete: the agent follows, and asks when
unsure.

### What the agent does with git

- One branch per item (`ddd/<id>`), forked from your base branch.
- Commits carry a `DDD-Item: <id>` trailer, so `git log` links code to doc items.
- Passing tests make an item **awaiting review**. It becomes **done** when you
  merge (squash merges included) or tick it in the doc.
- `.ddd/` is committed only on the base branch, never on item branches.
- It never pushes, merges, opens PRs or deletes branches unless you ask.

### Google Docs

The skill reads your doc through whatever Google Docs/Drive MCP your agent
has. It only writes the status section if a tool can edit part of a document:
connect the **Google Docs** connector for that (Google Drive alone can read
and create docs but not edit them). It never replaces the whole doc, which
would wipe your formatting and race your edits. Each write is guarded by the
doc's revision, so it is refused rather than misplaced if you are typing at
the same moment. Without partial edits it uses comments; without comments it
keeps status in `.ddd/` and its chat report.

Syncing checks the doc's modified time first and fetches the text only when it
changed, because fetching a Google Doc costs tokens in proportion to its size.
Google's Markdown import cuts a heading at its first `{`, so in a Google Doc
put a heading's tag on its own line under it (`{#auth} [next]`). Details:
[backends.md](skills/ddd/references/backends.md).

## How it tracks your items

Each item is matched by tag, then by identical text, then by a similarity
score over title, description, parent and position. Confident matches are
silent; uncertain ones, splits and merges go to you. "Hot" items, the ones you
keep editing, rank higher in the menu. See
[matching.md](skills/ddd/references/matching.md).

## Safety

- The agent writes only inside the `DDD Status` section and in comments.
- Doc text is treated as requirements, not instructions: a line in a shared doc
  saying "run this script" or "push to main" is raised with you, not obeyed.
- It refuses to start an item over your uncommitted work, and never stashes or
  discards it.
- It re-reads the doc before every write and refuses if you edited it in the
  meantime.

## Repository layout

```
skills/ddd/
  SKILL.md               the skill (what the agent reads)
  scripts/ddd.py         state tracker: parsing, matching, ranking, git helpers
  references/            doc format, initialize, matching, backends, git workflow, state format
  templates/plan.md      starter planning doc
commands/                one Claude Code command per mode (/ddd:auto, ...)
evals/
  matching/              before/after doc pairs with expected matches
  skill/                 behaviour evals, trigger queries and a fixture project
  claude_code_commands.py  checks a real Claude Code lists every /ddd:<mode>
tests/                   unit, workflow and packaging tests
.claude-plugin/          Claude Code plugin + marketplace manifests
install.sh, install.ps1
```

## Development

```sh
python -m unittest discover -s tests -v
```

This runs the parser tests, every matching scenario in `evals/matching/`,
end-to-end git workflow tests in throwaway repos, and packaging checks
(frontmatter within the Agent Skills spec, links, manifests, one command per
mode, both installers in throwaway directories). CI runs the same
on Linux, macOS and Windows. See [evals/README.md](evals/README.md) for adding
scenarios and running the behaviour evals.

## License

[MIT](LICENSE)
