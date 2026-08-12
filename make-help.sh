#!/usr/bin/env bash
# Regenerates the 'help' index from the cheat files in this repo.
# Convention: line 1 of every cheat file is '## <description>'.
set -euo pipefail
cd "$(dirname "$0")"

SKIP=" CNAME README.md help make-help.sh "

{
  echo '## This is the list of cheats that are accessible via curl operations.dev/<name>'
  echo
  for f in *; do
    [ -f "$f" ] || continue
    case "$SKIP" in *" $f "*) continue ;; esac
    case "$f" in *.html|*.sh|*.ps1) continue ;; esac
    # The claude pathway indexes itself: `claude` is the one entry point
    # listed here; its per-OS pages stay reachable by URL but unlisted, so
    # help shows one line instead of three. New claude-* pages inherit this.
    case "$f" in claude-*) continue ;; esac
    desc=$(head -1 "$f" | sed -E 's/^#+[[:space:]]*//; s/[[:space:]#]+$//')
    printf 'operations.dev/%-31s- %s\n' "$f" "$desc"
  done
} > help
