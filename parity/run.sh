#!/usr/bin/env bash
# Run one parity comparison between two local images.
#
# Starts the baseline and the candidate on a private docker network with the
# same dummy env file, then runs the parity client in a THIRD container on that
# network, so a server that only listens on 127.0.0.1 inside its own container
# is refused exactly the way a LAN client would refuse it.
#
# Inputs (environment):
#   PARITY_BASELINE_IMAGE   image ref of the last release (already pulled)
#   PARITY_CANDIDATE_IMAGE  image ref built from the change under test
#   PARITY_BASELINE_LABEL   the baseline's X.Y.Z version
#   PARITY_CLIENT_IMAGE     the parity client image (parity/Dockerfile)
#   PARITY_PORT             container port the server listens on
#   PARITY_MCP_PATH         MCP endpoint path, default /mcp
#   PARITY_GOLDEN           golden file on the host
#   PARITY_EXPECTED         expected-changes file on the host (may be absent)
#   PARITY_ENV_FILE         dummy env file for both servers and the client (optional)
#   PARITY_WAIT             seconds to wait for each server, default 60
#   PARITY_OUT              output directory: summary.md, verdict.txt, snapshots
#
# Exit status is the client's: 0 pass, 1 parity failure, 2 error. The last line
# of verdict.txt is always written, so no outcome is silent.

set -euo pipefail

: "${PARITY_BASELINE_IMAGE:?}" "${PARITY_CANDIDATE_IMAGE:?}" "${PARITY_BASELINE_LABEL:?}"
: "${PARITY_CLIENT_IMAGE:?}" "${PARITY_PORT:?}" "${PARITY_GOLDEN:?}" "${PARITY_EXPECTED:?}"
: "${PARITY_OUT:?}"
mcp_path="${PARITY_MCP_PATH:-/mcp}"
wait_seconds="${PARITY_WAIT:-60}"

mkdir -p "$PARITY_OUT/input"
out_dir="$(cd "$PARITY_OUT" && pwd)"
verdict="$out_dir/verdict.txt"
rm -f "$verdict" "$out_dir/summary.md"

error() {
  echo "parity: ERROR $*" | tee "$verdict"
  exit 2
}

case "$mcp_path" in /*) ;; *) error "mcp path must start with /, got $mcp_path" ;; esac
case "$PARITY_PORT" in '' | *[!0-9]*) error "port must be a number, got $PARITY_PORT" ;; esac

[ -f "$PARITY_GOLDEN" ] || error "golden file $PARITY_GOLDEN not found"
cp "$PARITY_GOLDEN" "$out_dir/input/golden.yaml"
rm -f "$out_dir/input/expected.yaml"
if [ -f "$PARITY_EXPECTED" ]; then
  cp "$PARITY_EXPECTED" "$out_dir/input/expected.yaml"
fi

env_args=()
if [ -n "${PARITY_ENV_FILE:-}" ]; then
  [ -f "$PARITY_ENV_FILE" ] || error "env file $PARITY_ENV_FILE not found"
  env_args=(--env-file "$PARITY_ENV_FILE")
fi

prefix="parity-$$-$RANDOM"
net="$prefix-net"
# shellcheck disable=SC2329 # invoked by the EXIT trap below
cleanup() {
  docker rm -f "$prefix-baseline" "$prefix-candidate" >/dev/null 2>&1 || true
  docker network rm "$net" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "$net" >/dev/null
docker run -d --name "$prefix-baseline" --network "$net" \
  ${env_args[@]+"${env_args[@]}"} "$PARITY_BASELINE_IMAGE" >/dev/null
docker run -d --name "$prefix-candidate" --network "$net" \
  ${env_args[@]+"${env_args[@]}"} "$PARITY_CANDIDATE_IMAGE" >/dev/null

rc=0
docker run --rm --network "$net" --user "$(id -u):$(id -g)" \
  ${env_args[@]+"${env_args[@]}"} \
  -v "$out_dir:/work" \
  "$PARITY_CLIENT_IMAGE" compare \
  --baseline-url "http://$prefix-baseline:$PARITY_PORT$mcp_path" \
  --candidate-url "http://$prefix-candidate:$PARITY_PORT$mcp_path" \
  --baseline-label "$PARITY_BASELINE_LABEL" \
  --golden /work/input/golden.yaml \
  --expected /work/input/expected.yaml \
  --wait "$wait_seconds" \
  --out /work || rc=$?

if [ "$rc" -ne 0 ]; then
  for role in baseline candidate; do
    echo "::group::$role container state and last 40 log lines"
    docker inspect --format '{{.Name}} status={{.State.Status}} exit={{.State.ExitCode}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$prefix-$role" || true
    docker logs --tail 40 "$prefix-$role" 2>&1 || true
    echo "::endgroup::"
  done
fi

if [ ! -s "$verdict" ]; then
  echo "parity: ERROR the client exited $rc without a verdict" | tee "$verdict"
  rc=2
fi
if [ -n "${GITHUB_STEP_SUMMARY:-}" ] && [ -f "$out_dir/summary.md" ]; then
  cat "$out_dir/summary.md" >>"$GITHUB_STEP_SUMMARY"
fi
exit "$rc"
