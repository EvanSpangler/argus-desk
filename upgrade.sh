#!/usr/bin/env bash
# argus-desk upgrade: replace ~/argus-desk with a newer repository zip, check it with gate0, roll back by itself
# if the checks fail. Every line it prints starts with UPGRADE (or GATE0 from gate0.sh). Nothing is deleted:
# the old folder is renamed argus-desk.prev-<stamp>, a failed new folder argus-desk.failed-<stamp>.
#
#   bash ~/argus-desk/upgrade.sh --check                 read-only: installed version, newest zip in ~/.cache,
#                                                        whether this host can fetch the zip itself
#   bash ~/argus-desk/upgrade.sh --fetch                 download main.zip from GitHub into ~/.cache, then install it
#   bash ~/argus-desk/upgrade.sh --from-zip <path>       install a zip you downloaded and uploaded yourself
#   bash ~/argus-desk/upgrade.sh --rollback              put the newest argus-desk.prev-* back
#   add --no-gate to skip gate0 (then there is no automatic rollback); --keep-zip leaves the zip in place
#
# After UPGRADE OK restart Claude Code (/exit, then your launcher) so the new version loads.
set -u
REPO="${ARGUS_DESK_REPO:-EvanSpangler/argus-desk}"
BRANCH="${ARGUS_DESK_BRANCH:-main}"
ZIP_URL="https://codeload.github.com/$REPO/zip/refs/heads/$BRANCH"
TARGET="${ARGUS_DESK_HOME:-$HOME/argus-desk}"
CACHE="$HOME/.cache"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
say() { printf 'UPGRADE %s\n' "$*"; }
die() { say "FAILED: $*"; exit 1; }

# The script lives inside the folder it replaces. Run from a copy so the rename under it changes nothing.
if [ -z "${ARGUS_DESK_UPGRADE_COPY:-}" ]; then
  C="$(mktemp "${TMPDIR:-/tmp}/argus-desk-upgrade.XXXXXX")" || die "no temp file"
  cp "${BASH_SOURCE[0]}" "$C" && ARGUS_DESK_UPGRADE_COPY=1 exec bash "$C" "$@"
  die "could not copy myself"
fi

ver_in() { sed -n 's/.*"version": *"\([0-9][0-9.]*\)".*/\1/p' | head -1; }   # plugin.json text -> version
version_of() { # folder -> version, "unknown" or "none"
  [ -f "$1/.claude-plugin/plugin.json" ] || { echo none; return; }
  local v; v="$(ver_in < "$1/.claude-plugin/plugin.json")"; echo "${v:-unknown}"
}
zip_version() { # zip -> version inside it; fails when the zip is not the repository
  local top v; top="$(unzip -Z1 "$1" 2>/dev/null | head -1 | cut -d/ -f1)"
  [ -n "$top" ] || return 1
  v="$(unzip -p "$1" "$top/.claude-plugin/plugin.json" 2>/dev/null | ver_in)"
  [ -n "$v" ] || return 1
  echo "$v"
}
newest_zip() { ls -t "$CACHE"/argus-desk-main*.zip 2>/dev/null | head -1; }
can_fetch() { curl -fsSIL -m 15 -o /dev/null -w '%{http_code}' "$ZIP_URL" 2>/dev/null || echo "no"; }

MODE="" ZIP="" GATE=1 KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check|--fetch|--rollback) MODE="${1#--}" ;;
    --from-zip) MODE=zip; ZIP="${2:-}"; shift ;;
    --no-gate) GATE=0 ;;
    --keep-zip) KEEP=1 ;;
    *) die "unknown argument $1" ;;
  esac
  shift
done
[ -n "$MODE" ] || die "say --check, --fetch, --from-zip <path> or --rollback"

say "host $(hostname) user $(id -un) date $STAMP"
say "installed: $(version_of "$TARGET") at $TARGET"

if [ "$MODE" = check ]; then
  Z="$(newest_zip)"
  if [ -n "$Z" ]; then say "newest zip: $Z (version $(zip_version "$Z" || echo unreadable))"; else say "newest zip: none in $CACHE"; fi
  say "fetch from GitHub: HTTP $(can_fetch) for $ZIP_URL (200 means --fetch works here; anything else means download in a browser and upload)"
  say "previous folders: $(ls -d "$TARGET".prev-* 2>/dev/null | wc -l | tr -d ' ')"
  say "END"
  exit 0
fi

if [ "$MODE" = rollback ]; then
  P="$(ls -td "$TARGET".prev-* 2>/dev/null | head -1)"
  [ -n "$P" ] || die "no $TARGET.prev-* to roll back to"
  if [ -e "$TARGET" ]; then mv "$TARGET" "$TARGET.failed-$STAMP" || die "could not move $TARGET aside"; say "moved aside: $TARGET.failed-$STAMP"; fi
  mv "$P" "$TARGET" || die "could not restore $P"
  say "ROLLED BACK to $(version_of "$TARGET") from $P. Restart Claude Code (/exit, then your launcher)."
  exit 0
fi

if [ "$MODE" = fetch ]; then
  mkdir -p "$CACHE"
  ZIP="$CACHE/argus-desk-main-$STAMP.zip"
  say "fetching $ZIP_URL"
  curl -fsSL -m 120 -o "$ZIP" "$ZIP_URL" || die "download failed (HTTP $(can_fetch)); download it in a browser and run --from-zip"
fi

[ -f "$ZIP" ] || die "no zip at ${ZIP:-<empty>}"
NEW="$(zip_version "$ZIP")" || die "$ZIP does not hold <folder>/.claude-plugin/plugin.json; is it the repository zip?"
OLD="$(version_of "$TARGET")"
TOP="$(unzip -Z1 "$ZIP" | head -1 | cut -d/ -f1)"
say "zip: $ZIP (version $NEW, top folder $TOP)"
[ "$NEW" != "$OLD" ] || say "note: same version $OLD; installing anyway"

WORK="$(mktemp -d "$HOME/.argus-desk-upgrade.XXXXXX")" || die "no work folder under $HOME"
unzip -q -o "$ZIP" -d "$WORK" || die "unzip failed"
[ -d "$WORK/$TOP" ] || die "unzip produced no $TOP"
if [ -e "$TARGET" ]; then
  PREV="$TARGET.prev-$STAMP"
  mv "$TARGET" "$PREV" || die "could not rename $TARGET"
  say "prev: $PREV"
else
  PREV=""
fi
mv "$WORK/$TOP" "$TARGET" || { [ -n "$PREV" ] && mv "$PREV" "$TARGET"; die "could not place the new folder"; }
rmdir "$WORK" 2>/dev/null
[ "$KEEP" = 1 ] || { [ "$MODE" = fetch ] && rm -f "$ZIP"; }
say "installed: $OLD -> $(version_of "$TARGET")"

if [ "$GATE" = 1 ]; then
  G="$(bash "$TARGET/gate0.sh" 2>&1)"
  echo "$G" | grep -E '^GATE0 (validate|plugin test|python tests|settings)' | sed 's/^/  /'
  OK=1
  echo "$G" | grep -qE '^GATE0 validate [✔√✓]' || OK=0
  echo "$G" | grep -qE '^GATE0 plugin test +[0-9]+ pass +0 fail' || OK=0
  echo "$G" | grep -qE '^GATE0 python tests OK' || OK=0
  if [ "$OK" = 0 ]; then
    FAILED="$TARGET.failed-$STAMP"
    mv "$TARGET" "$FAILED"
    [ -n "$PREV" ] && mv "$PREV" "$TARGET"
    say "ROLLED BACK: gate0 did not pass on $NEW; it is kept at $FAILED, $OLD is back. Send the GATE0 lines above to whoever ships the mod."
    exit 1
  fi
fi
say "OK $(version_of "$TARGET"). Restart Claude Code now (/exit, then your launcher) so it loads; then /desk."
say "END"
