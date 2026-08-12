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
NODE_MAJOR_WANTED=20     # what we install when the distro's is too old
NODE_MAJOR_MIN=18        # what Claude Code actually requires

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

node_ok() {
  have node || return 1
  [ "$(node -v | sed 's/^v\([0-9]*\).*/\1/')" -ge "$NODE_MAJOR_MIN" ] 2>/dev/null
}

ensure_node() {
  # Empirical, not a version table. Distro node ranges from ancient (Ubuntu
  # 22.04 ships v12) to perfectly fine (Ubuntu 24.04, Fedora, Rocky 10), and
  # which is which changes every release. So: try the distro, measure what we
  # actually got, and reach for NodeSource only if it is genuinely too old.
  node_ok && { note "node $(node -v) already present"; return 0; }

  note "installing the distro's nodejs"
  pkg_install nodejs 2>/dev/null || pkg_install node 2>/dev/null || true
  node_ok && { note "distro node $(node -v) is new enough"; return 0; }

  note "distro node is $(node -v 2>/dev/null || echo absent) - need ${NODE_MAJOR_MIN}+, adding NodeSource ${NODE_MAJOR_WANTED}"
  case "$PKG" in
    apt)
      curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR_WANTED}.x" | $SUDO -E bash -
      pkg_install nodejs ;;
    dnf|yum)
      # A module stream owns nodejs on RHEL 8/9 and NodeSource conflicts with
      # it until reset. Harmless on releases that have no module streams.
      $SUDO "$PKG" -y module reset nodejs >/dev/null 2>&1 || true
      curl -fsSL "https://rpm.nodesource.com/setup_${NODE_MAJOR_WANTED}.x" | $SUDO bash -
      pkg_install nodejs ;;
  esac

  node_ok || die "could not reach node ${NODE_MAJOR_MIN}+ (got: $(node -v 2>/dev/null || echo none)).
      NodeSource may not publish for this release yet. Install node ${NODE_MAJOR_MIN}+ by
      hand (nodejs.org, nvm, or a distro module stream) and re-run - the rest will skip."
}


install_gh() {
  # gh is missing from RHEL-family defaults and inconsistent across Ubuntu
  # releases, so try the distro first and add GitHub's repo only if needed.
  if pkg_install gh 2>/dev/null && have gh; then return 0; fi
  note "gh not in the distro repos - adding GitHub's"
  case "$PKG" in
    apt)
      $SUDO mkdir -p -m 755 /etc/apt/keyrings
      curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | $SUDO tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
      $SUDO chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        | $SUDO tee /etc/apt/sources.list.d/github-cli.list >/dev/null
      pkg_install gh ;;
    dnf|yum)
      # Write the .repo file directly instead of using config-manager: dnf5
      # (Fedora 41+, likely Rocky 10) renamed --add-repo to addrepo, and the
      # plugin is not always installed. Dropping the file works on all of them.
      curl -fsSL https://cli.github.com/packages/rpm/gh-cli.repo \
        | $SUDO tee /etc/yum.repos.d/gh-cli.repo >/dev/null
      pkg_install gh ;;
    *)
      die "install the GitHub CLI by hand (cli.github.com), then re-run" ;;
  esac
}


# ---------------------------------------------------------------- packages
say "Checking prerequisites"
have git || { note "installing git"; pkg_install git; }
have gh  || { note "installing the GitHub CLI"; install_gh; }
ensure_node
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
