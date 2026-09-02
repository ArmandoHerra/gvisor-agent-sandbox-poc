#!/usr/bin/env bash
# capture-logs.sh — Session orchestrator for container log capture
# Generates a session ID, creates a log directory, launches background
# log/event collectors, runs the agent container, collects exit state,
# and merges logs for post-mortem analysis.
#
# Usage:
#   ./scripts/capture-logs.sh \
#     --mode <direct|proxied> \
#     --interactive <true|false> \
#     --log-dir <path> \
#     --agent-image <image:tag> \
#     --runtime <runsc> \
#     --memory <2g> \
#     --cpus <2> \
#     --pids-limit <100> \
#     --tmp-size <100m> \
#     --work-size <500m> \
#     [--proxy-name <anthropic-proxy>] \
#     [--proxy-net <proxy-net>] \
#     [--proxy-port <18080>]

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
MODE="direct"
INTERACTIVE="false"
LOG_BASE_DIR="logs"
AGENT_IMAGE="claude-agent:latest"
RUNTIME="runsc"
MEMORY="2g"
CPUS="2"
PIDS_LIMIT="100"
TMP_SIZE="100m"
WORK_SIZE="500m"
PROXY_NAME="anthropic-proxy"
PROXY_NET="proxy-net"
PROXY_PORT="18080"
LOG_TIMESTAMPS="${LOG_TIMESTAMPS:-1}"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)         MODE="$2";          shift 2 ;;
    --interactive)  INTERACTIVE="$2";   shift 2 ;;
    --log-dir)      LOG_BASE_DIR="$2";  shift 2 ;;
    --agent-image)  AGENT_IMAGE="$2";   shift 2 ;;
    --runtime)      RUNTIME="$2";       shift 2 ;;
    --memory)       MEMORY="$2";        shift 2 ;;
    --cpus)         CPUS="$2";          shift 2 ;;
    --pids-limit)   PIDS_LIMIT="$2";    shift 2 ;;
    --tmp-size)     TMP_SIZE="$2";      shift 2 ;;
    --work-size)    WORK_SIZE="$2";     shift 2 ;;
    --proxy-name)   PROXY_NAME="$2";    shift 2 ;;
    --proxy-net)    PROXY_NET="$2";     shift 2 ;;
    --proxy-port)   PROXY_PORT="$2";    shift 2 ;;
    --) shift; break ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Session ID and directories
# ---------------------------------------------------------------------------
TIMESTAMP="$(date -u '+%Y%m%d-%H%M%S')"

if [[ "$INTERACTIVE" == "true" ]]; then
  if [[ "$MODE" == "proxied" ]]; then
    SESSION_ID="${TIMESTAMP}-interactive-proxied"
  else
    SESSION_ID="${TIMESTAMP}-interactive"
  fi
else
  SESSION_ID="${TIMESTAMP}-${MODE}"
fi

SESSION_DIR="${LOG_BASE_DIR}/${SESSION_ID}"
mkdir -p "${SESSION_DIR}"

# Create / update the 'latest' symlink
ln -sfn "${SESSION_ID}" "${LOG_BASE_DIR}/latest"

echo "[capture] Session: ${SESSION_ID}"
echo "[capture] Log dir: ${SESSION_DIR}"

# ---------------------------------------------------------------------------
# Host info
# ---------------------------------------------------------------------------
HOST_HOSTNAME="$(hostname)"
KERNEL_VERSION="$(uname -r)"
DOCKER_VERSION="$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo 'unknown')"

# ---------------------------------------------------------------------------
# Write initial metadata.json
# ---------------------------------------------------------------------------
STARTED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
AGENT_CONTAINER_NAME="claude-agent-${SESSION_ID}"

# Determine proxy running state
PROXY_RUNNING_AT_START="false"
if [[ "$MODE" == "proxied" ]]; then
  if docker inspect -f '{{.State.Running}}' "${PROXY_NAME}" 2>/dev/null | grep -q true; then
    PROXY_RUNNING_AT_START="true"
  fi
fi

cat > "${SESSION_DIR}/metadata.json" <<EOF
{
  "session_id": "${SESSION_ID}",
  "mode": "${MODE}",
  "interactive": ${INTERACTIVE},
  "started_at": "${STARTED_AT}",
  "ended_at": null,
  "duration_seconds": null,
  "agent_container": "${AGENT_CONTAINER_NAME}",
  "agent_exit_code": null,
  "proxy_container": "${PROXY_NAME}",
  "proxy_running_at_start": ${PROXY_RUNNING_AT_START},
  "runtime": "${RUNTIME}",
  "memory_limit": "${MEMORY}",
  "cpus": "${CPUS}",
  "pids_limit": "${PIDS_LIMIT}",
  "image": "${AGENT_IMAGE}",
  "proxy_image": "anthropic-proxy:latest",
  "host": {
    "hostname": "${HOST_HOSTNAME}",
    "kernel": "${KERNEL_VERSION}",
    "docker_version": "${DOCKER_VERSION}"
  },
  "log_files": [
    "agent.log",
    "events.log"
  ]
}
EOF

# ---------------------------------------------------------------------------
# Background process tracking
# ---------------------------------------------------------------------------
BG_PIDS=()

cleanup() {
  echo ""
  echo "[capture] Cleaning up background collectors..."
  for pid in "${BG_PIDS[@]:-}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  # Remove the agent container if it still exists
  if docker inspect "${AGENT_CONTAINER_NAME}" >/dev/null 2>&1; then
    echo "[capture] Removing agent container ${AGENT_CONTAINER_NAME}..."
    docker rm -f "${AGENT_CONTAINER_NAME}" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Proxied mode: verify proxy is running and get IP
# ---------------------------------------------------------------------------
PROXY_IP=""
if [[ "$MODE" == "proxied" ]]; then
  if ! docker inspect -f '{{.State.Running}}' "${PROXY_NAME}" 2>/dev/null | grep -q true; then
    echo "[capture] ERROR: Proxy container '${PROXY_NAME}' is not running." >&2
    echo "[capture] Start it first with: make start-proxy" >&2
    exit 1
  fi

  # Capture proxy logs in background
  echo "[capture] Starting proxy log collector..."
  docker logs --follow --timestamps "${PROXY_NAME}" >> "${SESSION_DIR}/proxy.log" 2>&1 &
  BG_PIDS+=($!)

  # Get proxy IP on the internal network
  PROXY_IP="$(docker inspect -f '{{json .NetworkSettings.Networks}}' "${PROXY_NAME}" \
    | python3 -c "import sys,json; nets=json.load(sys.stdin); print(nets['${PROXY_NET}']['IPAddress'])")"
  echo "[capture] Proxy IP on ${PROXY_NET}: ${PROXY_IP}"
fi

# ---------------------------------------------------------------------------
# Docker events collector
# ---------------------------------------------------------------------------
echo "[capture] Starting docker events collector..."

EVENT_FILTER_ARGS=(--filter "container=${AGENT_CONTAINER_NAME}")
if [[ "$MODE" == "proxied" ]]; then
  EVENT_FILTER_ARGS+=(--filter "container=${PROXY_NAME}")
fi

docker events \
  "${EVENT_FILTER_ARGS[@]}" \
  --format '{{json .}}' \
  >> "${SESSION_DIR}/events.log" 2>&1 &
BG_PIDS+=($!)

# ---------------------------------------------------------------------------
# Build common docker run flags (no --rm: we do manual cleanup)
# ---------------------------------------------------------------------------
DOCKER_FLAGS=(
  --runtime="${RUNTIME}"
  --name "${AGENT_CONTAINER_NAME}"
  --cap-drop ALL
  --security-opt no-new-privileges
  --read-only
  --tmpfs "/tmp:rw,noexec,nosuid,size=${TMP_SIZE}"
  --tmpfs "/workspace:rw,noexec,nosuid,size=${WORK_SIZE}"
  --memory "${MEMORY}"
  --cpus "${CPUS}"
  --pids-limit "${PIDS_LIMIT}"
  --user 1000:1000
  -e PYTHONUNBUFFERED=1
  -e ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-}"
)

if [[ "$MODE" == "proxied" ]]; then
  DOCKER_FLAGS+=(
    --network="${PROXY_NET}"
    --add-host="proxy-host:${PROXY_IP}"
    -e ANTHROPIC_PROXY_URL="http://proxy-host:${PROXY_PORT}"
    -e ANTHROPIC_API_KEY="proxied"
  )
else
  DOCKER_FLAGS+=(-e ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}")
fi

# ---------------------------------------------------------------------------
# Run the agent container
# ---------------------------------------------------------------------------
AGENT_EXIT_CODE=0

if [[ "$INTERACTIVE" == "true" ]]; then
  # Interactive mode: use `script` to capture TTY output while preserving interactivity
  if command -v script >/dev/null 2>&1; then
    echo "[capture] Starting interactive session (script -q)..."
    # Build the docker command as a string for script -c
    DOCKER_CMD="docker run ${DOCKER_FLAGS[*]} -it ${AGENT_IMAGE} --interactive"
    script -q -c "${DOCKER_CMD}" "${SESSION_DIR}/agent.log" || AGENT_EXIT_CODE=$?
  else
    echo "[capture] WARNING: 'script' not found — running without log capture for interactive mode." >&2
    docker run "${DOCKER_FLAGS[@]}" -it "${AGENT_IMAGE}" --interactive || AGENT_EXIT_CODE=$?
  fi
else
  # Non-interactive (probe) mode: pipe through tee for real-time + persisted output
  echo "[capture] Starting probe session..."
  if [[ "${LOG_TIMESTAMPS:-1}" == "1" ]] && command -v stdbuf >/dev/null 2>&1; then
    # Inject ISO 8601 timestamps into each line for merge-log sorting.
    # Source tag ([agent]) is NOT added here — merge-logs.sh adds it during merge.
    stdbuf -oL docker run "${DOCKER_FLAGS[@]}" "${AGENT_IMAGE}" 2>&1 \
      | TZ=UTC awk '{ print strftime("[%Y-%m-%dT%H:%M:%SZ]"), $0; fflush() }' \
      | tee "${SESSION_DIR}/agent.log" \
      || true
    # PIPESTATUS is not available after a pipe with ||; capture exit code from docker inspect below
  else
    docker run "${DOCKER_FLAGS[@]}" "${AGENT_IMAGE}" 2>&1 \
      | tee "${SESSION_DIR}/agent.log" \
      || true
  fi
fi

# ---------------------------------------------------------------------------
# Collect agent exit code from container inspect (since we didn't use --rm)
# ---------------------------------------------------------------------------
sleep 0.5  # brief wait for container state to settle
if docker inspect "${AGENT_CONTAINER_NAME}" >/dev/null 2>&1; then
  AGENT_EXIT_CODE="$(docker inspect "${AGENT_CONTAINER_NAME}" --format '{{.State.ExitCode}}' 2>/dev/null || echo '0')"
fi

echo "[capture] Agent exit code: ${AGENT_EXIT_CODE}"

# ---------------------------------------------------------------------------
# Stop background collectors
# ---------------------------------------------------------------------------
for pid in "${BG_PIDS[@]:-}"; do
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
  fi
done
BG_PIDS=()

# Give collectors a moment to flush
sleep 0.5

# ---------------------------------------------------------------------------
# Update metadata.json with final state
# ---------------------------------------------------------------------------
ENDED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# Compute duration in seconds
START_EPOCH="$(date -u -d "${STARTED_AT}" '+%s' 2>/dev/null || date -j -f '%Y-%m-%dT%H:%M:%SZ' "${STARTED_AT}" '+%s' 2>/dev/null || echo 0)"
END_EPOCH="$(date -u '+%s')"
DURATION=$(( END_EPOCH - START_EPOCH ))

# Determine which log files exist (session.log will be generated by merge-logs.sh below)
LOG_FILES='["agent.log", "events.log"'
if [[ -f "${SESSION_DIR}/proxy.log" ]]; then
  LOG_FILES="${LOG_FILES}, \"proxy.log\""
fi
LOG_FILES="${LOG_FILES}, \"session.log\"]"

PROXY_RUNNING_LINE='"proxy_container": "'"${PROXY_NAME}"'",'
if [[ "$MODE" == "proxied" ]]; then
  PROXY_RUNNING_LINE="${PROXY_RUNNING_LINE}
  \"proxy_running_at_start\": true,"
else
  PROXY_RUNNING_LINE="${PROXY_RUNNING_LINE}
  \"proxy_running_at_start\": false,"
fi

cat > "${SESSION_DIR}/metadata.json" <<EOF
{
  "session_id": "${SESSION_ID}",
  "mode": "${MODE}",
  "interactive": ${INTERACTIVE},
  "started_at": "${STARTED_AT}",
  "ended_at": "${ENDED_AT}",
  "duration_seconds": ${DURATION},
  "agent_container": "${AGENT_CONTAINER_NAME}",
  "agent_exit_code": ${AGENT_EXIT_CODE},
  ${PROXY_RUNNING_LINE}
  "runtime": "${RUNTIME}",
  "memory_limit": "${MEMORY}",
  "cpus": "${CPUS}",
  "pids_limit": "${PIDS_LIMIT}",
  "image": "${AGENT_IMAGE}",
  "proxy_image": "anthropic-proxy:latest",
  "host": {
    "hostname": "${HOST_HOSTNAME}",
    "kernel": "${KERNEL_VERSION}",
    "docker_version": "${DOCKER_VERSION}"
  },
  "log_files": ${LOG_FILES}
}
EOF

# ---------------------------------------------------------------------------
# Generate merged session.log
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/merge-logs.sh" ]]; then
  echo "[capture] Merging logs into session.log..."
  "${SCRIPT_DIR}/merge-logs.sh" "${SESSION_DIR}"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "[capture] =========================================="
echo "[capture] Session complete"
echo "[capture] Session ID : ${SESSION_ID}"
echo "[capture] Log dir    : ${SESSION_DIR}"
echo "[capture] Exit code  : ${AGENT_EXIT_CODE}"
echo "[capture] Duration   : ${DURATION}s"
echo "[capture] =========================================="
