# Changelog

## [Task: FEAT-002 Container Sandbox Logging & Capture] - 2026-03-12

### Added
- `logs/.gitkeep` — tracks the logs directory structure in git; session log contents are gitignored
- `scripts/capture-logs.sh` — core session orchestration script: generates session IDs, creates per-session log directories, launches background `docker events` and `docker logs --follow` collectors, runs the agent container (non-interactive via `stdbuf -oL ... | tee`; interactive via `script -q -c`), collects exit code from `docker inspect`, updates `metadata.json` with full session state, invokes `merge-logs.sh`, and removes the agent container on exit
- `scripts/merge-logs.sh` — post-mortem log merger: reads `agent.log`, `proxy.log` (optional), and `events.log`, prefixes each line with `[agent]`/`[proxy]`/`[events]` source tags, sorts timestamped lines lexicographically (ISO 8601), groups non-timestamped lines at the end, and writes `session.log`
- Makefile logged run targets: `run-logged`, `run-proxied-logged`, `prompt-logged`, `prompt-proxied-logged` — parallel variants of base targets that invoke `capture-logs.sh` with all sandbox flags
- Makefile review/maintenance targets: `logs-list`, `logs-latest`, `logs-review`, `logs-events`, `logs-merge`, `logs-clean`, `logs-clean-all`
- Makefile variables: `LOG_DIR`, `LOG_CAPTURE`, `LOG_MERGE`, `LOG_RETENTION_DAYS`
- `.env.example` logging vars: `LOG_DIR`, `LOG_RETENTION_DAYS`, `SANDBOX_LOG_ENABLED`, `LOG_TIMESTAMPS`
- README.md "Logging" section with session layout diagram, post-mortem workflow, crash detection notes, and configuration reference

### Changed
- `.gitignore` — added `logs/*` and `!logs/.gitkeep` rules
- `Makefile` `.PHONY` declaration extended with all new targets
- `README.md` — updated project structure, Available Commands table (added logged and review targets), and added full Logging section
- `scripts/capture-logs.sh` awk timestamp injection format: timestamps are injected as `[YYYY-MM-DDThh:mm:ssZ] <output>` without an embedded source tag — source tags are added exclusively by `merge-logs.sh` to avoid double-tagging

### Notes
- All logging is shell-based — no Python changes, no Docker image changes, no new dependencies
- Agent containers in logged mode do NOT use `--rm`; the capture script removes them explicitly with `docker rm -f` after capture is complete
- `PYTHONUNBUFFERED=1` is set as a container env var in logged mode to prevent stdout buffering losses on crash
- `stdbuf -oL` is used in the non-interactive pipeline for line-buffered tee output
- Interactive mode uses `script -q -c` (util-linux, standard on Linux); falls back to unlogged run with a warning if `script` is not available
- `logs-events` target uses `jq` if available, falls back to `python3 -m json.tool`, then raw `cat`
- `logs-list` uses `jq` if available, falls back to `python3` inline for JSON field extraction
- Session IDs with `ended_at: null` in `metadata.json` are flagged as `INCOMPLETE` by `logs-list`

## [Task: FEAT-002 Validation — Bug Fixes] - 2026-03-12

### Fixed
- `scripts/capture-logs.sh` — awk `strftime` timestamp injection now uses `TZ=UTC` prefix to produce correct UTC timestamps in `agent.log`. Without this fix, timestamps used local time (CST), causing agent lines to sort before proxy/events lines in `session.log` despite occurring later chronologically.
- `scripts/capture-logs.sh` — `log_files` field in final `metadata.json` now always includes `session.log`. Previously the check for `session.log` existence ran before `merge-logs.sh` was invoked, so the file was never found and never included in the field.
- `scripts/merge-logs.sh` — Docker events JSON lines (format: `{"status":"...","time":<epoch>,...}`) now have their Unix epoch `time` field extracted and converted to ISO 8601 UTC for correct sort ordering in `session.log`. Previously these lines matched no timestamp pattern and fell into the `[no-ts]` bucket, appearing at the bottom of the merged log instead of in correct temporal position.

### Notes
- Validation ran all non-interactive test targets: `run`, `run-proxied`, `run-logged`, `run-proxied-logged`, `logs-list`, `logs-latest`, `logs-review`, `logs-events`, `logs-merge`, `logs-clean`
- All 4 runs produced complete session directories with correct file sets, metadata, and temporal ordering
- No orphaned containers found after any run
- `make prompt` and `make prompt-proxied` verified by Makefile inspection (not run — require interactive TTY)
