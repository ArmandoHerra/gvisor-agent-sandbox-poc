#!/usr/bin/env bash
# merge-logs.sh — Post-mortem log merger
# Merges agent.log, proxy.log (if present), and events.log from a session
# directory into a time-sorted session.log. Each line is prefixed with its
# source tag: [agent], [proxy], or [events].
#
# Usage:
#   ./scripts/merge-logs.sh <session-dir>
#
# Output:
#   <session-dir>/session.log

set -euo pipefail

# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------
if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <session-dir>" >&2
  exit 1
fi

SESSION_DIR="$1"

if [[ ! -d "${SESSION_DIR}" ]]; then
  echo "ERROR: Session directory not found: ${SESSION_DIR}" >&2
  exit 1
fi

SESSION_LOG="${SESSION_DIR}/session.log"
TMP_MERGED="${SESSION_DIR}/.merge-tmp-$$"

# Ensure temp file is cleaned up on exit
trap 'rm -f "${TMP_MERGED}"' EXIT

# ---------------------------------------------------------------------------
# Helper: prefix lines from a log file with a source tag
# Lines that already start with a timestamp bracket (from awk injection or
# docker logs --timestamps) are kept as-is and sorted by that timestamp.
# Lines without a leading timestamp are grouped with the preceding entry.
# ---------------------------------------------------------------------------
prefix_lines() {
  local file="$1"
  local tag="$2"

  if [[ ! -f "${file}" ]]; then
    return
  fi

  # For each line, prepend the source tag
  # If the line already has a leading timestamp like [2026-...] or
  # an ISO timestamp from docker logs (2026-03-12T...), keep it first
  # so sorting works correctly. Otherwise prefix with tag only.
  awk -v tag="${tag}" '
  {
    # Check if line starts with a timestamp in brackets: [YYYY-MM-DD...]
    if (match($0, /^\[20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9:Z.]+\]/)) {
      # Already has timestamp bracket — insert source tag after it
      ts = substr($0, RSTART, RLENGTH)
      rest = substr($0, RSTART + RLENGTH)
      print ts " " tag " " rest
    } else if (match($0, /^20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9:.]+Z? /)) {
      # Docker logs --timestamps format: 2026-03-12T14:30:22.001234567Z message
      ts_end = RSTART + RLENGTH - 1
      ts = substr($0, RSTART, RLENGTH - 1)
      rest = substr($0, RSTART + RLENGTH)
      print "[" ts "]" " " tag " " rest
    } else if (match($0, /"time":([0-9]+)/)) {
      # Docker events JSON format: {"status":"...","time":1773300261,...}
      # Extract the epoch timestamp and convert to ISO 8601 UTC
      epoch = substr($0, RSTART + 7, RLENGTH - 7)
      ts = strftime("%Y-%m-%dT%H:%M:%SZ", epoch + 0, 1)
      print "[" ts "] " tag " " $0
    } else {
      # No timestamp — output without timestamp prefix so it sorts last / stays in order
      print "[no-ts] " tag " " $0
    }
    fflush()
  }
  ' "${file}"
}

# ---------------------------------------------------------------------------
# Collect and prefix all available log files
# ---------------------------------------------------------------------------
> "${TMP_MERGED}"

prefix_lines "${SESSION_DIR}/agent.log"  "[agent]"  >> "${TMP_MERGED}" 2>/dev/null || true
prefix_lines "${SESSION_DIR}/proxy.log"  "[proxy]"  >> "${TMP_MERGED}" 2>/dev/null || true
prefix_lines "${SESSION_DIR}/events.log" "[events]" >> "${TMP_MERGED}" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Sort: lines with timestamps sort lexicographically (ISO 8601 is sort-safe)
# Lines prefixed with [no-ts] are appended at the end grouped by source
# ---------------------------------------------------------------------------
# Separate timestamped and non-timestamped lines
TMP_TS="${SESSION_DIR}/.merge-ts-$$"
TMP_NOTS="${SESSION_DIR}/.merge-nots-$$"
trap 'rm -f "${TMP_MERGED}" "${TMP_TS}" "${TMP_NOTS}"' EXIT

grep -v '^\[no-ts\]' "${TMP_MERGED}" | sort -s -k1,1 > "${TMP_TS}" 2>/dev/null || true
grep '^\[no-ts\]'    "${TMP_MERGED}"                  > "${TMP_NOTS}" 2>/dev/null || true

# Combine: timestamped (sorted) first, then non-timestamped in their original order
cat "${TMP_TS}" "${TMP_NOTS}" > "${SESSION_LOG}"

# Clean up temp files
rm -f "${TMP_MERGED}" "${TMP_TS}" "${TMP_NOTS}"
trap - EXIT

LINE_COUNT="$(wc -l < "${SESSION_LOG}")"
echo "[merge] Written ${LINE_COUNT} lines to ${SESSION_LOG}"
