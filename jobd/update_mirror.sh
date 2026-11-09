#!/usr/bin/env bash
# jobd/update_mirror.sh
# Keep a bare mirror of the DUNE-DAQ GitHub repos for a native daq-jobd host
# (see jobd/README.md, "Native hosts"). Meant for cron, e.g. hourly:
#   17 * * * * $HOME/daq-jobd/dunedaq-container/jobd/update_mirror.sh
#
# Usage: update_mirror.sh [MIRROR_DIR]     (default ~/dunedaq_repos_mirror)
#
# Each repo is MIRROR_DIR/<repo>.git. GitHub's branches are fetched into
# refs/remotes/origin/* and its tags into refs/tags/*, with --prune. refs/heads/*
# is left to pushes from your own machine (the `daqfnal` remote), so the hourly
# update never moves or deletes a branch you pushed.
#
# Anonymous HTTPS only: no GitHub key needed. Archived repos and *-firmware repos
# are skipped (MIRROR_SKIP, an extended regex, overrides the latter). Runs under
# flock, so overlapping cron runs just exit. Log: MIRROR_DIR/update.log.

set -uo pipefail

MIRROR="${1:-$HOME/dunedaq_repos_mirror}"
SKIP="${MIRROR_SKIP:--firmware$}"
ORG="DUNE-DAQ"
LOG="$MIRROR/update.log"

mkdir -p "$MIRROR"
exec 9>"$MIRROR/.lock"
flock -n 9 || exit 0

if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 5000000 ]; then mv "$LOG" "$LOG.old"; fi
exec >>"$LOG" 2>&1
echo "===== $(date '+%Y-%m-%d %H:%M:%S') ====="

org_repos() {  # non-archived public repos, one per line
  local page=1 out
  while :; do
    out=$(curl -sf --max-time 30 "https://api.github.com/orgs/$ORG/repos?type=public&per_page=100&page=$page") || return 1
    out=$(printf '%s' "$out" | jq -r '.[] | select(.archived | not) | .name') || return 1
    [ -n "$out" ] || return 0
    printf '%s\n' "$out"
    page=$((page + 1))
  done
}

new_mirror() {  # new_mirror REPO
  local d="$MIRROR/$1.git"
  git init -q --bare "$d" &&
    git -C "$d" remote add origin "https://github.com/$ORG/$1.git" &&
    git -C "$d" config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*' &&
    git -C "$d" config --add remote.origin.fetch '+refs/tags/*:refs/tags/*' &&
    git -C "$d" config remote.origin.tagOpt --no-tags
}

if repos=$(org_repos); then
  for r in $repos; do
    [[ "$r" =~ $SKIP ]] && continue
    if [ ! -d "$MIRROR/$r.git" ]; then
      echo "new: $r"
      new_mirror "$r" || { echo "FAILED to create $r"; rm -rf "$MIRROR/$r.git"; }
    fi
  done
else
  echo "warning: GitHub API request failed; updating existing mirrors only"
fi

ok=0 failed=()
for d in "$MIRROR"/*.git; do
  [ -d "$d" ] || continue
  if git -C "$d" fetch -q --prune origin; then ok=$((ok + 1)); else failed+=("$(basename "$d" .git)"); fi
done
echo "updated $ok, failed ${#failed[@]}${failed[*]:+: ${failed[*]}}"
[ ${#failed[@]} -eq 0 ]
