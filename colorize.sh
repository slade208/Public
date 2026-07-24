#!/usr/bin/env bash
# Emit an ANSI-colored version of a cheat file to stdout.
# Used by the GitHub Action at publish time; repo sources stay plain.
#   '## heading'  -> bold yellow
#   '# comment'   -> dim
#   index lines   -> cyan URL, dim dash
# Usage: colorize.sh <file>

B=$'\e[1m'
CYAN=$'\e[36m'
YELLOW=$'\e[33m'
DIM=$'\e[2m'
R=$'\e[0m'

sed -E \
  -e "s|^##.*$|${B}${YELLOW}&${R}|" \
  -e "s|^#([^#].*)?$|${DIM}&${R}|" \
  -e "s|^(operations\.dev/[A-Za-z0-9_-]+)( +)- (.*)$|${CYAN}\1${R}\2${DIM}-${R} \3|" \
  "$1"
