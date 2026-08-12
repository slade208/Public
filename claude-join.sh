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
# Distro-aware: works on Debian/Ubuntu, RHEL/Rocky/Alma/Fedora, and macOS.
# Idempotent: every step checks first, so re-running after a failure is safe.
# Full docs: github.com/slade208/jarvis-agent docs/runbooks/join-a-seat-to-shared-knowledge.md
set -euo pipefail

TREE="${AGENT_NOTES_DIR:-$HOME/agent-notes}"
REPO="${AGENT_NOTES_REPO:-https://github.com/slade208/agent-notes.git}"
SEAT="${AGENT_NOTES_SEAT:-$(hostname)}"
NODE_MAJOR_WANTED=20

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[1;31mx %s\033[0m\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# Prompts must come from the terminal, not from whatever stdin happens to be.
ask() { local p="$1" d="${2:-}" a; read -r -p "$p" a </dev/tty || a=""; printf '%s' "${a:-$d}"; }

# Root already; otherwise sudo. A minimal container may have neither.
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  have sudo || die "not root and no sudo — install git, gh and node 18+ by hand, then re-run"
  SUDO="sudo"
fi

# ------------------------------------------------------------ identify the OS
# NOT `uname` (that's the kernel — every distro says "Linux") and NOT
# /etc/issue (an editable login banner, frequently blank). /etc/os-release is
# the systemd standard and is present on every distro this script targets.
OS="$(uname -s)"
DISTRO="unknown"; PRETTY="$OS"
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  DISTRO="${ID:-unknown}"
  PRETTY="${PRETTY_NAME:-$DISTRO}"
  LIKE="${ID_LIKE:-}"
fi

# What we actually need is the package manager, not the distro name.
PKG=""
if   have apt-get; then PKG=apt
elif have dnf;     then PKG=dnf
elif have yum;     then PKG=yum
elif have brew;    then PKG=brew
elif have zypper;  then PKG=zypper
fi

say "System"
note "$PRETTY  (package manager: ${PKG:-none found})"

pkg_install() {
  case "$PKG" in
    apt)    $SUDO apt-get update -qq && $SUDO apt-get install -y "$@" ;;
    dnf)    $SUDO dnf install -y "$@" ;;
    yum)    $SUDO yum install -y "$@" ;;
    zypper) $SUDO zypper --non-interactive install "$@" ;;
    brew)   brew install "$@" ;;
    *)      die "no supported package manager — install $* by hand, then re-run" ;;
  esac
}

install_node() {
  note "installing Node ${NODE_MAJOR_WANTED} (distro packages are usually too old for Claude Code)"
  case "$PKG" in
    apt)
      curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR_WANTED}.x" | $SUDO -E bash -
      pkg_install nodejs ;;
    dnf|yum)
      curl -fsSL "https://rpm.nodesource.com/setup_${NODE_MAJOR_WANTED}.x" | $SUDO bash -
      pkg_install nodejs ;;
    *)
      pkg_install node || pkg_install nodejs ;;
  esac
}

install_gh() {
  # gh is absent from RHEL-family default repos and inconsistent across Ubuntu
  # releases, so: try the distro first, add GitHub's own repo only if needed.
  if pkg_install gh 2>/dev/null; then return 0; fi
  note "gh not in the distro repos — adding GitHub's"
  case "$PKG" in
    apt)
      $SUDO mkdir -p -m 755 /etc/apt/keyrings
      curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | $SUDO tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
      $SUDO chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        | $SUDO tee /etc/apt/sources.list.d/github-cli.list >/dev/null
      pkg_install gh ;;
    dnf)
      $SUDO dnf install -y 'dnf-command(config-manager)' || true
      $SUDO dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo
      pkg_install gh ;;
    yum)
      $SUDO yum install -y yum-utils || true
      $SUDO yum-config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo
      pkg_install gh ;;
    *)
      die "install the GitHub CLI by hand (cli.github.com), then re-run" ;;
  esac
}

# ---------------------------------------------------------------- packages
say "Checking prerequisites"
have git || { note "installing git"; pkg_install git; }
have gh  || { note "installing the GitHub CLI"; install_gh; }
if have node; then
  NODE_MAJOR=$(node -v | sed 's/^v\([0-9]*\).*/\1/')
  if [ "${NODE_MAJOR:-0}" -lt 18 ]; then
    note "node $(node -v) is too old (need 18+)"
    install_node
  fi
else
  install_node
fi
note "git $(git --version | awk '{print $3}') · gh $(gh --version | head -1 | awk '{print $3}') · node $(node -v)"

# ------------------------------------------------------------- claude code
say "Claude Code"
if have claude; then
  note "already installed"
else
  # npm's global prefix is root-owned on distro node; keep the install unprivileged.
  npm install -g @anthropic-ai/claude-code 2>/dev/null \
    || $SUDO npm install -g @anthropic-ai/claude-code
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
PY_BIN=""
have python3 && PY_BIN=python3 || { have python && PY_BIN=python; }
if [ -n "$PY_BIN" ]; then
  SETTINGS="$SETTINGS" TREE="$TREE" "$PY_BIN" - <<'PY'
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
  note "no python found — add the hooks by hand, see: curl operations.dev/claude-linux"
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
