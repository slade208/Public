#!/usr/bin/env bash
# Bootstrap a Linux/macOS box into a Claude Code seat on the shared knowledge tree.
#
#   bash -c "$(curl -fsSL operations.dev/claude-join.sh)"
#
# Run it that way, NOT `curl ... | bash`. Piping makes the script itself occupy
# stdin, so the two interactive logins (gh, claude) cannot read your keyboard.
# The bash -c "$(...)" form passes the script as an argument and leaves your
# terminal connected — same reason Homebrew's installer is written that way.
#
# Idempotent: every step checks first, so re-running after a failure is safe.
# Full docs: github.com/slade208/jarvis-agent docs/runbooks/join-a-seat-to-shared-knowledge.md
set -euo pipefail

TREE="${AGENT_NOTES_DIR:-$HOME/agent-notes}"
REPO="${AGENT_NOTES_REPO:-https://github.com/slade208/agent-notes.git}"
SEAT="${AGENT_NOTES_SEAT:-$(hostname)}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[1;31mx %s\033[0m\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# Prompts must come from the terminal, not from whatever stdin happens to be.
ask() { local p="$1" d="${2:-}" a; read -r -p "$p" a </dev/tty || a=""; printf '%s' "${a:-$d}"; }

[ "$(uname -s)" = "Linux" ] || [ "$(uname -s)" = "Darwin" ] || die "this script is for Linux/macOS; Windows: irm operations.dev/claude-win-join.ps1 | iex"

# ---------------------------------------------------------------- packages
say "Checking prerequisites"
NEED=""
have git || NEED="$NEED git"
have gh  || NEED="$NEED gh"
if have node; then
  NODE_MAJOR=$(node -v | sed 's/^v\([0-9]*\).*/\1/')
  [ "${NODE_MAJOR:-0}" -ge 18 ] || NEED="$NEED nodejs"
else
  NEED="$NEED nodejs"
fi

if [ -n "$NEED" ]; then
  note "missing:$NEED"
  if have apt-get; then
    case "$NEED" in *nodejs*)
      note "adding the NodeSource 20 repo (Ubuntu's own node is too old for Claude Code)"
      curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - ;;
    esac
    # shellcheck disable=SC2086
    sudo apt-get install -y $NEED
  elif have brew; then
    # shellcheck disable=SC2086
    brew install $NEED
  else
    die "no apt-get or brew here — install:$NEED by hand, then re-run"
  fi
else
  note "git, gh and node 18+ all present"
fi

# ------------------------------------------------------------- claude code
say "Claude Code"
if have claude; then
  note "already installed ($(claude --version 2>/dev/null | head -1))"
else
  npm install -g @anthropic-ai/claude-code
fi

# -------------------------------------------------------------------- auth
say "GitHub access (the knowledge tree is a private repo)"
if gh auth status >/dev/null 2>&1; then
  note "already authenticated"
else
  note "a browser or device code is needed for this step"
  gh auth login
fi
gh auth setup-git

# ------------------------------------------------------------------- clone
say "Knowledge tree"
if [ -d "$TREE/.git" ]; then
  note "already at $TREE — pulling"
  git -C "$TREE" pull --rebase --autostash --quiet || note "pull failed; using the local copy"
else
  git clone "$REPO" "$TREE"
fi
note "$(ls "$TREE/internal"/*.md 2>/dev/null | wc -l) facts in the tree"

# ----------------------------------------------------------------- project
say "Which project should this seat use?"
DEFAULT_PROJECT="$HOME/projects/$(basename "$PWD")"
[ "$PWD" = "$HOME" ] && DEFAULT_PROJECT="$HOME/projects/scratch"
PROJECT=$(ask "    path [$DEFAULT_PROJECT]: " "$DEFAULT_PROJECT")
mkdir -p "$PROJECT"
note "using $PROJECT"

# --------------------------------------------------------------------- join
say "Joining this seat to the tree"
AGENT_NOTES_DIR="$TREE" bash "$TREE/tooling/join.sh" "$PROJECT" "$SEAT"

# -------------------------------------------------------------------- hooks
say "Session hooks"
SETTINGS="$PROJECT/.claude/settings.json"
mkdir -p "$(dirname "$SETTINGS")"
if have python3; then
  SETTINGS="$SETTINGS" TREE="$TREE" python3 - <<'PY'
import json, os, pathlib
p = pathlib.Path(os.environ["SETTINGS"]); tree = os.environ["TREE"]
cfg = {}
if p.exists():
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        backup = p.with_suffix(".json.bak")
        p.rename(backup)                      # never silently discard a broken file
        print(f"    existing settings.json was invalid; kept it as {backup.name}")
hooks = cfg.setdefault("hooks", {})
for event, script in (("SessionStart", "session-start.sh"), ("Stop", "stop.sh")):
    cmd = f'bash {tree}/tooling/hooks/{script}'
    entries = hooks.setdefault(event, [])
    if any(cmd in json.dumps(e) for e in entries):
        print(f"    {event}: already wired")
        continue
    entries.append({"hooks": [{"type": "command", "shell": "bash", "command": cmd}]})
    print(f"    {event}: added")
p.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
PY
else
  note "no python3 — add the hooks by hand, see: curl operations.dev/claude-linux"
fi

# ------------------------------------------------------------------ finish
say "Done — one thing left for you"
note "1. Log in to Claude Code if you haven't on this box:   claude"
note "2. Then, in $PROJECT, ask it:"
note "      \"what do you remember?\""
note "   A joined seat lists facts grouped by topic; empty means the link failed."
printf '\n'
note "seat name: $SEAT   (export AGENT_NOTES_SEAT to change it)"
note "tree:      $TREE"
note "full docs: github.com/slade208/agent-notes -> JOINING.md"
