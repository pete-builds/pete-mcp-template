#!/usr/bin/env bash
# Checks parity/verdict.sh, the reusable workflow's last step: only a PASS line
# or the first-release skip line may exit 0; a FAIL, an ERROR, an empty file
# and a missing file must each exit non-zero.

set -u

here="$(cd "$(dirname "$0")" && pwd)"
verdict_sh="$here/../verdict.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
failed=0

# case <name> <want exit> <file content, or "@missing" for no file>
case_() {
  local name="$1" want="$2" content="$3" file="$tmp/$1.txt"
  if [ "$content" != "@missing" ]; then
    printf '%s' "$content" >"$file"
  fi
  local out rc=0
  out="$(GITHUB_OUTPUT="" GITHUB_STEP_SUMMARY="" bash "$verdict_sh" "$file")" || rc=$?
  local ok=yes
  [ "$rc" -eq "$want" ] || { ok=no; failed=1; }
  printf '  %-12s want exit %s, got %s  %-3s %s\n' "$name" "$want" "$rc" "$ok" "$out"
}

echo "verdict.sh checks:"
case_ pass 0 "parity: PASS against baseline 0.1.0 (1 tools, 0 golden calls, 0 declared differences)
"
case_ skip 0 "parity: no baseline image, first release, skipped
"
case_ fail 1 "parity: FAIL 1 undeclared difference(s) against baseline 0.1.0
"
case_ error 1 "parity: ERROR resolving baseline: denied
"
case_ empty 1 ""
case_ missing 1 "@missing"
case_ lastline 1 "parity: PASS against baseline 0.1.0 (1 tools)
parity: FAIL candidate unreachable over the docker network (baseline 0.1.0)
"

if [ "$failed" -ne 0 ]; then
  echo "verdict.sh checks: FAIL"
  exit 1
fi
echo "verdict.sh checks: PASS (7 of 7)"
