#!/usr/bin/env bash
# Install the glab-overseer skill into the Claude Code and Antigravity skill roots.
#
#   install.sh [--claude-dir D] [--agy-dir D] [--only claude|agy] [--link] [--dry-run] [--uninstall]
#
# D is a skills root: the skill lands in D/glab-overseer. Defaults: ~/.claude/skills and
# ~/.gemini/config/skills. A directory is replaced or removed only when it holds the
# .glab-overseer-install marker that this script wrote.
set -eu

NAME=glab-overseer
MARKER=.glab-overseer-install
HERE=$(cd "$(dirname "$0")" && pwd)
SRC="$HERE/skills/$NAME"

claude_dir="${HOME:-}/.claude/skills"
agy_dir="${HOME:-}/.gemini/config/skills"
only=""
link=0
dry=0
uninstall=0

usage() {
  echo "usage: install.sh [--claude-dir D] [--agy-dir D] [--only claude|agy] [--link] [--dry-run] [--uninstall]"
}
die() { echo "install.sh: $*" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --claude-dir) [ $# -ge 2 ] || die "--claude-dir needs a directory"; claude_dir=$2; shift 2 ;;
    --agy-dir) [ $# -ge 2 ] || die "--agy-dir needs a directory"; agy_dir=$2; shift 2 ;;
    --only) [ $# -ge 2 ] || die "--only needs claude or agy"; only=$2; shift 2 ;;
    --link) link=1; shift ;;
    --dry-run) dry=1; shift ;;
    --uninstall) uninstall=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done
case "$only" in ""|claude|agy) ;; *) die "--only must be claude or agy" ;; esac
[ -n "$claude_dir" ] && [ -n "$agy_dir" ] || die "no skills directory: set HOME or pass --claude-dir and --agy-dir"
[ -f "$SRC/SKILL.md" ] || die "cannot find $SRC/SKILL.md; run install.sh from a checkout"

abs() { case "$1" in /*) printf '%s\n' "${1%/}" ;; *) printf '%s\n' "$PWD/${1%/}" ;; esac; }

targets=""
[ "$only" = agy ] || targets="$(abs "$claude_dir")/$NAME"
[ "$only" = claude ] || targets="$targets
$(abs "$agy_dir")/$NAME"
targets=$(printf '%s\n' "$targets" | sed '/^$/d')
IFS='
'  # targets are newline separated, so paths with spaces work

marked() { [ -d "$1" ] && [ ! -L "$1" ] && [ -f "$1/$MARKER" ] && [ ! -L "$1/$MARKER" ]; }
mode_of() { sed -n 's/^mode=//p' "$1/$MARKER" | head -n 1; }

same_as_source() {  # $1 target, $2 mode
  [ "$(mode_of "$1")" = "$2" ] || return 1
  if [ "$2" = copy ]; then
    diff -r -q --exclude="$MARKER" --exclude=__pycache__ "$SRC" "$1" >/dev/null 2>&1
  else
    for e in "$SRC"/*; do
      [ "$(readlink "$1/$(basename "$e")" 2>/dev/null)" = "$e" ] || return 1
    done
    [ "$(ls -A "$1" | grep -vc "^$MARKER\$")" -eq "$(ls -A "$SRC" | wc -l)" ]
  fi
}

build() {  # $1 destination directory (new), $2 mode
  mkdir -p "$1"
  if [ "$2" = copy ]; then
    cp -R "$SRC/." "$1/"
    find "$1" -name __pycache__ -type d -prune -exec rm -rf {} +
  else
    for e in "$SRC"/*; do ln -s "$e" "$1/$(basename "$e")"; done
  fi
  printf 'glab-overseer install\nmode=%s\n' "$2" > "$1/$MARKER"
}

rc=0
mode=copy
[ "$link" -eq 0 ] || mode=link

if [ "$uninstall" -eq 1 ]; then
  for t in $targets; do
    if [ ! -e "$t" ] && [ ! -L "$t" ]; then
      echo "not installed: $t"
    elif marked "$t"; then
      if [ "$dry" -eq 1 ]; then echo "would remove: $t"; else rm -rf "$t"; echo "removed: $t"; fi
    else
      echo "refusing to remove $t: no $MARKER marker (not created by this installer)" >&2
      rc=1
    fi
  done
  exit $rc
fi

# Check every target before changing any.
for t in $targets; do
  if { [ -e "$t" ] || [ -L "$t" ]; } && ! marked "$t"; then
    echo "refusing to overwrite $t: no $MARKER marker (not created by this installer)" >&2
    rc=1
  fi
done
[ "$rc" -eq 0 ] || exit 1

for t in $targets; do
  if marked "$t" && same_as_source "$t" "$mode"; then
    echo "unchanged: $t ($mode)"
    continue
  fi
  verb=install
  marked "$t" && verb=update
  if [ "$dry" -eq 1 ]; then echo "would $verb ($mode): $t"; continue; fi
  parent=$(dirname "$t")
  mkdir -p "$parent"
  new="$parent/.$NAME.new.$$"
  rm -rf "$new"
  build "$new" "$mode"
  if marked "$t"; then rm -rf "$t"; fi
  mv "$new" "$t"
  echo "$verb ($mode): $t"
done
