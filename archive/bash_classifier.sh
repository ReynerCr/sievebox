#!/usr/bin/env bash
# Harness: run the bash discovery classifier on synthetic trace data.
# Usage: bash_classifier.sh <trace> <bound> <tmpfs> <here> <path_env>
# Output: failures rows on stdout (tab-separated), same format as failures.log.
set -euo pipefail

TRACE="$1"; BOUND="$2"; TMPFS="$3"; HERE="$4"; PATHENV="$5"

REPO="$(cd "$(dirname "$0")/../.." && pwd)"

export HERE HOME="/home/user" TARGET_BIN="node" EFFECTIVE_DEPS="node webdev gui"
export SIEVEBOX_CONFIG="$REPO/archive/sievebox-profiles.sh" SIEVEBOX_STATE_DIR="/tmp/sievebox-test"
export PATH="$PATHENV"

extract_bound_paths() { :; }
extract_tmpfs_paths() { :; }

source "$REPO/archive/sievebox-discovery.sh"

failures=$(mktemp)
probing=$(mktemp)
: > "$failures"; : > "$probing"

_discovery_classify "$TRACE" "$BOUND" "$TMPFS" "$failures" "$probing"
_discovery_mark_exists "$failures"

cat "$failures"
rm -f "$failures" "$probing"
