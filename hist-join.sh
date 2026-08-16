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
if [ ! -f "$KEY" ]; then
    ssh-keygen -t ed25519 -N "" -f "$KEY" -C "shell-history@$HOSTTAG" >/dev/null
    say "generated deploy key for $HOSTTAG"
fi

if ! GIT_SSH_COMMAND="$SSHCMD" git ls-remote "$REPO_SSH" >/dev/null 2>&1; then
    say "repo not reachable with this host's key yet."
    say "add the key below as a DEPLOY KEY (write access) on slade208/shell-history,"
    say "then re-run this script:"
    echo
    cat "$KEY.pub"
    exit 2
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
