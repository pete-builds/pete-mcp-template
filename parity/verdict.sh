#!/usr/bin/env bash
# Print the parity check's one final line and exit on it.
#
# Usage: verdict.sh <verdict-file>
#
# Exits 0 only for "parity: PASS ..." or the first-release skip line. A missing
# or empty verdict file means a step stopped before the check reached a
# verdict, and that must never read as a pass.

set -u

verdict_file="${1:?usage: verdict.sh <verdict-file>}"
line=""
if [ -s "$verdict_file" ]; then
  line="$(tail -n 1 "$verdict_file")"
fi
if [ -z "$line" ]; then
  line="parity: ERROR the check stopped before reaching a verdict (see the failed step above)"
fi

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "verdict=$line" >>"$GITHUB_OUTPUT"
fi
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  printf '\n**%s**\n' "$line" >>"$GITHUB_STEP_SUMMARY"
fi
echo "$line"

case "$line" in
  "parity: PASS "* | "parity: no baseline image, first release, skipped") exit 0 ;;
  *) exit 1 ;;
esac
