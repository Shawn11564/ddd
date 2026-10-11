#!/usr/bin/env sh
# Install the ddd skill by copying skills/ddd into an agent's skills folder.
#
#   ./install.sh                  # auto: Claude Code and/or Codex, whichever is present
#   ./install.sh --target claude  # ~/.claude/skills/ddd, plus /ddd:<mode> commands in ~/.claude/commands/ddd
#   ./install.sh --target codex   # ~/.agents/skills/ddd  (Codex and other Agent Skills hosts)
#   ./install.sh --target project # ./.claude/skills/ddd (+ commands) and ./.agents/skills/ddd in the current repo
#   ./install.sh --dir PATH       # PATH/ddd (skill only)
#
# Run from a clone, or pipe from the web (it then clones the repo to a temp dir):
#   curl -fsSL https://raw.githubusercontent.com/Shawn11564/ddd/main/install.sh | sh
set -eu

REPO_URL="${DDD_REPO_URL:-https://github.com/Shawn11564/ddd.git}"
TARGET="auto"
DIR=""

while [ $# -gt 0 ]; do
  case "$1" in
    --target) TARGET="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || echo "")
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/skills/ddd/SKILL.md" ]; then
  SRC="$SCRIPT_DIR/skills/ddd"
  CMDS="$SCRIPT_DIR/commands"
else
  command -v git >/dev/null 2>&1 || { echo "git is required to download ddd" >&2; exit 1; }
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  git clone --quiet --depth 1 "$REPO_URL" "$TMP/ddd"
  SRC="$TMP/ddd/skills/ddd"
  CMDS="$TMP/ddd/commands"
fi

install_to() {
  dest="$1/ddd"
  mkdir -p "$1"
  if [ -d "$dest" ]; then
    rm -rf "$dest"
    action="updated"
  else
    action="installed"
  fi
  cp -R "$SRC" "$dest"
  find "$dest" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  echo "$action $dest"
}

# Claude Code: one command per mode (/ddd:auto, /ddd:status, ...) so the modes
# show up in the slash-command menu. Overwrites only ddd's own files.
install_commands() {
  dest="$1/ddd"
  mkdir -p "$dest"
  cp "$CMDS"/*.md "$dest/"
  echo "installed commands in $dest"
}

if [ -n "$DIR" ]; then
  install_to "$DIR"
else
  case "$TARGET" in
    claude) install_to "$HOME/.claude/skills"; install_commands "$HOME/.claude/commands" ;;
    codex|agents) install_to "$HOME/.agents/skills" ;;
    project) install_to "./.claude/skills"; install_commands "./.claude/commands"; install_to "./.agents/skills" ;;
    auto)
      done_any=""
      if [ -d "$HOME/.claude" ]; then
        install_to "$HOME/.claude/skills"; install_commands "$HOME/.claude/commands"; done_any=1
      fi
      if [ -d "$HOME/.codex" ] || [ -d "$HOME/.agents" ]; then install_to "$HOME/.agents/skills"; done_any=1; fi
      if [ -z "$done_any" ]; then install_to "$HOME/.claude/skills"; install_commands "$HOME/.claude/commands"; fi
      ;;
    *) echo "unknown target: $TARGET (claude, codex, project)" >&2; exit 1 ;;
  esac
fi

if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
  echo "note: ddd needs Python 3.8+ on PATH (python3 or python)" >&2
fi
echo "Start a new agent session, then run /ddd or /ddd:<mode> (Claude Code) or ask for \"ddd\" (Codex)."
