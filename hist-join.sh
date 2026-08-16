#!/usr/bin/env bash
# shell-history enrol — one curl from any Linux host (BACKLOG #72).
#
#   bash -c "$(curl -fsSL https://operations.dev/hist-join.sh)"
#
# Two-phase by design: the first run generates a per-host deploy key and
# prints the public half; add it to github.com/slade208/shell-history as a
# deploy key WITH WRITE ACCESS, then run again to complete the enrolment.
# Idempotent — safe to re-run any time.
set -u

REPO_SSH="git@github.com:slade208/shell-history.git"
DIR="$HOME/.shell-history"
KEY="$HOME/.ssh/shell_history_ed25519"
HOSTTAG="$(hostname -s)"
SSHCMD="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

say() { printf '%s\n' "[hist-join] $*"; }
die() { printf '%s\n' "[hist-join] ERROR: $*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || die "git is required"
command -v ssh-keygen >/dev/null 2>&1 || die "ssh-keygen is required"
command -v flock >/dev/null 2>&1 || say "WARNING: flock missing - sync locking degraded"

# --- phase 1: per-host deploy key -------------------------------------------
# The host itself needs NO GitHub tooling - only git, curl, ssh-keygen. The
# deploy key must be registered on GitHub's side, and there are two ways:
#   - gh CLI on this box (optional): fully automatic, browserless-friendly
#     (gh auth login uses a device code you can approve from a phone)
#   - any browser anywhere: paste the printed key at the settings URL below
if [ ! -f "$KEY" ]; then
    ssh-keygen -t ed25519 -N "" -f "$KEY" -C "shell-history@$HOSTTAG" >/dev/null
    say "generated deploy key for $HOSTTAG"
fi

reachable() { GIT_SSH_COMMAND="$SSHCMD" git ls-remote "$REPO_SSH" >/dev/null 2>&1; }

ask_tty() {  # ask_tty "question" -> 0 on yes; non-interactive contexts refuse
    [ -r /dev/tty ] && [ -w /dev/tty ] || return 1
    printf '[hist-join] %s [y/N] ' "$1" > /dev/tty
    read -r _ans < /dev/tty || return 1
    case "$_ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

install_gh() {  # official packages, package-manager-agnostic (cli.github.com)
    _sudo=""; [ "$(id -u)" != "0" ] && _sudo="sudo"
    if command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
        _pm=dnf; command -v dnf >/dev/null 2>&1 || _pm=yum
        curl -fsSL https://cli.github.com/packages/rpm/gh-cli.repo | $_sudo tee /etc/yum.repos.d/gh-cli.repo >/dev/null \
            && $_sudo $_pm install -y gh
    elif command -v apt-get >/dev/null 2>&1; then
        $_sudo mkdir -p -m 755 /etc/apt/keyrings \
            && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | $_sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null \
            && $_sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
            && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | $_sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null \
            && $_sudo apt-get update -qq && $_sudo apt-get install -y -qq gh
    else
        say "no dnf/yum/apt found - install gh manually: https://cli.github.com"
        return 1
    fi
}

ensure_gh_ready() {  # returns 0 iff gh is installed AND authenticated
    if ! command -v gh >/dev/null 2>&1; then
        ask_tty "gh CLI not found - install it now to finish enrolment from this box?" || return 1
        install_gh || return 1
    fi
    gh auth status >/dev/null 2>&1 && return 0
    say "gh needs a one-time login (device flow: it prints a code + URL you"
    say "can open on your phone - no browser needed on this box)"
    ask_tty "run 'gh auth login' now?" || return 1
    gh auth login < /dev/tty > /dev/tty 2>&1
    gh auth status >/dev/null 2>&1
}

if ! reachable; then
    if ensure_gh_ready; then
        gh repo deploy-key add "$KEY.pub" -R slade208/shell-history --allow-write --title "$HOSTTAG" 2>/dev/null \
            && say "deploy key added via gh - continuing in this same run"
        sleep 2
        # least-privilege note: the deploy key is what enrolment uses from now
        # on; you may 'gh auth logout' afterwards if you don't want a broad
        # account token sitting on this host.
    fi
    if ! reachable; then
        say "repo not reachable with this host's key yet. The key:"
        echo
        cat "$KEY.pub"
        echo
        say "register it one of two ways, then re-run this script:"
        say "  A) browser (any device): https://github.com/slade208/shell-history/settings/keys/new"
        say "     title: $HOSTTAG - tick 'Allow write access'"
        say "  B) gh CLI on this box:   gh auth login   (device flow, phone-friendly)"
        say "     then just re-run this script - it adds the key itself."
        exit 2
    fi
fi

# --- phase 2: clone + host log ----------------------------------------------
if [ ! -d "$DIR/.git" ]; then
    GIT_SSH_COMMAND="$SSHCMD" git clone -q "$REPO_SSH" "$DIR" || die "clone failed"
    say "cloned to $DIR"
fi
cd "$DIR" || die "cannot cd $DIR"
git config core.sshCommand "$SSHCMD"
git config user.name "$HOSTTAG"
git config user.email "shell-history@$HOSTTAG.invalid"

if [ ! -f "hosts/$HOSTTAG.log" ]; then
    mkdir -p hosts generated
    touch "hosts/$HOSTTAG.log"
    git add "hosts/$HOSTTAG.log"
    git commit -q -m "enrol $HOSTTAG"
    git push -q || { git pull -q --rebase && git push -q; }
    say "host log hosts/$HOSTTAG.log registered"
fi

# --- bashrc block (marker-guarded, idempotent) ------------------------------
BASHRC="$HOME/.bashrc"
if ! grep -q '>>> shell-history >>>' "$BASHRC" 2>/dev/null; then
    cat >> "$BASHRC" <<'EOF'

# >>> shell-history >>>
# Estate-wide shared bash history (github.com/slade208/shell-history).
# Managed block - edit via the repo's bootstrap.sh, remove via leave.sh.
if [ -f "$HOME/.shell-history/hosts/$(hostname -s).log" ]; then
    export HISTFILE="$HOME/.shell-history/hosts/$(hostname -s).log"
    shopt -s histappend
    export HISTSIZE=1000000
    export HISTFILESIZE=2000000
    export HISTTIMEFORMAT='%F %T '
    export HISTIGNORE=' *:history:history *:*token=*:*password=*:*passwd=*:*secret=*:*api_key=*:*apikey=*'
    PROMPT_COMMAND="history -a${PROMPT_COMMAND:+; $PROMPT_COMMAND}"
    # hot set: other hosts' recent commands, loaded into this session's Ctrl-R
    _shw="$HOME/.shell-history/generated/window-excl-$(hostname -s).hist"
    [ -f "$_shw" ] && history -r "$_shw"
    unset _shw
fi
# <<< shell-history <<<
EOF
    say "bashrc block installed"
fi

# --- sync on logout + cron --------------------------------------------------
LOGOUT="$HOME/.bash_logout"
if ! grep -q '>>> shell-history >>>' "$LOGOUT" 2>/dev/null; then
    cat >> "$LOGOUT" <<'EOF'
# >>> shell-history >>>
[ -x "$HOME/.shell-history/tooling/hist-sync.sh" ] && "$HOME/.shell-history/tooling/hist-sync.sh" >/dev/null 2>&1
# <<< shell-history <<<
EOF
    say "logout sync installed"
fi

if command -v crontab >/dev/null 2>&1; then
    if ! crontab -l 2>/dev/null | grep -q 'shell-history/tooling/hist-sync.sh'; then
        ( crontab -l 2>/dev/null; echo "*/15 * * * * $HOME/.shell-history/tooling/hist-sync.sh >/dev/null 2>&1" ) | crontab - \
            && say "cron sync installed (every 15 min)" \
            || say "WARNING: could not install crontab entry - logout sync only"
    fi
else
    say "WARNING: no crontab on this host - logout sync only"
fi

say "enrolled. Open a new shell to start capturing; run tooling/histgrep to search the estate."
