#!/usr/bin/env bash
# Mutation controls for the parity check, run by this repo's CI.
#
# Takes a server rendered from the template, builds it as the "release", plants
# one fault per mutant in a copy of its source, confirms each plant landed, then
# runs parity/run.sh for every case and checks the verdict:
#
#   self        release vs itself                 must PASS
#   renamed     tool parameter renamed            must FAIL
#   truncated   tool description cut short        must FAIL
#   loopback    server binds 127.0.0.1 only       must FAIL (unreachable)
#   dropped     argument accepted, then ignored   must FAIL (golden call differs)
#   declared    renamed, declared in expected     must PASS, listing the declaration
#
# Usage: selftest.sh <rendered-server-dir> <parity-client-image> <work-dir>

set -euo pipefail

sample="$1"
client_image="$2"
work="$3"
here="$(cd "$(dirname "$0")" && pwd)"
run_sh="$here/../run.sh"
port=3800

[ -f "$sample/Dockerfile" ] || { echo "no Dockerfile in $sample"; exit 2; }
server_py="$(find "$sample/src" -name server.py | head -n 1)"
[ -n "$server_py" ] || { echo "no server.py under $sample/src"; exit 2; }
rel_server="${server_py#"$sample"/}"

mkdir -p "$work"

# plant <name> <file relative to the server root> <literal to find> <replacement>
# Copies the server, replaces the literal, and refuses to continue unless the
# literal was present before and the replacement is present after.
plant() {
  local name="$1" file="$2" find_text="$3" replace_text="$4"
  local dir="$work/src-$name"
  rm -rf "$dir"
  cp -R "$sample" "$dir"
  local before
  before="$(grep -cF -- "$find_text" "$dir/$file" || true)"
  if [ "$before" -lt 1 ]; then
    echo "PLANT FAILED: $name: '$find_text' not found in $file"
    exit 2
  fi
  FIND="$find_text" REPLACE="$replace_text" python3 - "$dir/$file" <<'PY'
import os, sys
p = sys.argv[1]
s = open(p).read()
open(p, "w").write(s.replace(os.environ["FIND"], os.environ["REPLACE"]))
PY
  local after_old after_new
  after_old="$(grep -cF -- "$find_text" "$dir/$file" || true)"
  after_new="$(grep -cF -- "$replace_text" "$dir/$file" || true)"
  echo "plant $name: '$find_text' x$before -> x$after_old remaining, '$replace_text' x$after_new in $file"
  if [ "$after_old" -ne 0 ] || [ "$after_new" -lt 1 ]; then
    echo "PLANT FAILED: $name did not land"
    exit 2
  fi
}

plant renamed "$rel_server" 'name: str = "world"' 'who: str = "world"'
# The renamed source must still be valid: fix the two other references.
FIND='{name}' REPLACE='{who}' python3 - "$work/src-renamed/$rel_server" <<'PY'
import os, sys
p = sys.argv[1]
s = open(p).read().replace(os.environ["FIND"], os.environ["REPLACE"])
s = s.replace("        name: Who to greet.", "        who: Who to greet.")
open(p, "w").write(s)
PY
grep -n 'who' "$work/src-renamed/$rel_server"

plant truncated "$rel_server" '"""Sample tool. Replace with your own.' '"""Sample tool.'
plant loopback Dockerfile 'MCP_HOST=0.0.0.0' 'MCP_HOST=127.0.0.1'
plant dropped "$rel_server" 'f"hello, {name}"' '"hello, " + "world"'

echo "::group::build images"
docker build -q -t parity-selftest:release "$sample"
for m in renamed truncated loopback dropped; do
  docker build -q -t "parity-selftest:$m" "$work/src-$m"
done
echo "::endgroup::"

env_file="$work/parity.env"
printf 'MCP_AUTH_TOKEN=parity-selftest-dummy\n' > "$env_file"

results=()
failed=0

# check <case> <candidate tag> <expected file> <want exit> <want verdict prefix> [<must also contain>]
check() {
  local name="$1" candidate="$2" expected="$3" want_rc="$4" want_prefix="$5" also="${6:-}"
  local out="$work/out-$name"
  rm -rf "$out"
  echo "::group::case $name (candidate parity-selftest:$candidate)"
  local rc=0
  PARITY_BASELINE_IMAGE=parity-selftest:release \
    PARITY_CANDIDATE_IMAGE="parity-selftest:$candidate" \
    PARITY_BASELINE_LABEL=0.1.0 \
    PARITY_CLIENT_IMAGE="$client_image" \
    PARITY_PORT="$port" \
    PARITY_GOLDEN="$here/golden.yaml" \
    PARITY_EXPECTED="$expected" \
    PARITY_ENV_FILE="$env_file" \
    PARITY_WAIT=30 \
    PARITY_OUT="$out" \
    GITHUB_STEP_SUMMARY="" \
    bash "$run_sh" > "$out.log" 2>&1 || rc=$?
  cat "$out.log"
  echo "::endgroup::"
  local verdict="(no verdict line)"
  if [ -s "$out/verdict.txt" ]; then
    verdict="$(tail -n 1 "$out/verdict.txt")"
  fi
  local ok=yes
  [ "$rc" -eq "$want_rc" ] || ok=no
  case "$verdict" in "$want_prefix"*) ;; *) ok=no ;; esac
  if [ -n "$also" ] && ! grep -qF -- "$also" "$out.log"; then
    ok=no
    verdict="$verdict [missing evidence: $also]"
  fi
  [ "$ok" = yes ] || failed=1
  results+=("$(printf '%-10s want exit %s, got %s  %-4s %s' "$name" "$want_rc" "$rc" "$ok" "$verdict")")
}

none="$work/no-expected.yaml"
rm -f "$none"

check self release "$none" 0 "parity: PASS against baseline 0.1.0"
check renamed renamed "$none" 1 "parity: FAIL" "tools.hello.inputSchema.properties.who"
check truncated truncated "$none" 1 "parity: FAIL" "tools.hello.description"
check loopback loopback "$none" 1 "parity: FAIL candidate unreachable over the docker network" \
  "Starting MCP server on 127.0.0.1:$port"
check dropped dropped "$none" 1 "parity: FAIL" "calls.hello-named"
check declared renamed "$here/expected-renamed.yaml" 0 "parity: PASS" \
  "Renamed the hello tool's argument from name to who"

echo
echo "parity self-test results:"
for r in "${results[@]}"; do
  echo "  $r"
done
if [ "$failed" -ne 0 ]; then
  echo "parity self-test: FAIL (a control did not behave as required)"
  exit 1
fi
echo "parity self-test: PASS (6 of 6 controls behaved as required)"
