# Changelog

## [First-time setup validation — dependency fixes] - 2026-09-01

### Fixed
- `make venv` could not run the test suite: it only installed `requirements-proxy.txt`
  (aiohttp), so `pytest` was never available. Added `requirements-dev.txt`
  (`-r requirements-proxy.txt` + anthropic, httpx2, pytest, pytest-aiohttp,
  pytest-asyncio) and pointed the `venv` target at it.
- `agent.py` crashed on import in a freshly built image: it imported `httpx`
  relying on it being a transitive dependency of the Anthropic SDK, but
  `anthropic` 1.3.0 switched its transport to `httpx2`, so `httpx` was no longer
  installed. Migrated `agent.py` to `import httpx2` (same exception names —
  `RemoteProtocolError`/`ReadError`) so the streaming error handlers match what
  the SDK's transport actually raises, and the Dockerfile now installs `httpx2`
  explicitly instead of depending on the SDK's dependency tree.
- Undeclared test dependency: `tests/test_proxy.py` uses the `aiohttp_client`
  fixture from `pytest-aiohttp`, which was never listed anywhere. Declared in
  `requirements-dev.txt`.
- **`make run` / `make prompt` echoed the real `ANTHROPIC_API_KEY` to the
  terminal**: the `docker run` recipe lines were not `@`-prefixed, so make
  printed the full command including `-e ANTHROPIC_API_KEY="sk-ant-..."` into
  scrollback (and anything capturing it). Both targets now run silenced with a
  sanitized status line instead; the proxied targets were already fully
  `@`-prefixed compound commands and never leaked.
- Refusal notice upgraded to print its evidence: the `model` field of the
  response (which model actually served the request) plus structured
  `stop_details` (category + explanation) — so a refusal is attributable and
  explainable, not just detected.
- `make proxy-logs` always printed nothing: the proxy's structured logs go to
  stderr (`logging.StreamHandler(sys.stderr)`), and the target's `2>/dev/null`
  — meant to suppress docker's missing-container error — discarded every log
  line. Now checks container existence first and shows both streams; added
  `make proxy-logs-follow` (`docker logs -f`) for watching a live session.
- Removed `enable_cleanup_closed=True` from the proxy's upstream `TCPConnector`
  (`proxy.py`): it worked around a CPython SSL-transport leak fixed in
  3.12.7+/3.13.1+ (cpython PR #118960); on current Pythons aiohttp ignores the
  flag and emitted a DeprecationWarning per app startup (13 across the test
  suite). Suite is now warning-free.

### Changed
- Agent model upgraded off the retired `claude-opus-4-6` and made configurable:
  `ANTHROPIC_MODEL` env var (default `claude-sonnet-5` — Opus/Fable decline the
  sandbox-probe prompt via safety classifiers; Sonnet 5 answers it cleanly and
  shares the same 128K output cap; empty falls back) threaded through both
  `agent.py` call
  sites, all four Makefile run targets, and `capture-logs.sh`; the active model
  now also appears in the probe's environment report.
- Added a `stop_reason == "refusal"` notice after both streams — Fable-family
  models can decline via safety classifiers with HTTP 200, which previously
  would have rendered as a silent empty response.
- `max_tokens` raised to a shared `MAX_TOKENS = 128000` constant in both call
  sites (probe was 512, REPL 32768) — the output cap common to every current
  Fable/Opus/Sonnet model; both sites stream, which the SDK requires at this
  size.

### Notes
- Found while walking the README top-to-bottom on a fresh machine as an external
  user would (guide-effectiveness test). Full suite green after fixes:
  29 passed on Python 3.14 / anthropic 1.3.0 / httpx2 2.12.0.

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
