# Changelog

## [Fix: OpenAI run_shell tool 400 on reasoning models] - 2026-09-02

### Fixed
- The model-callable `run_shell` tool failed on `gpt-5.6-sol` with
  `400 invalid_request_error`: "Function tools with reasoning_effort are not
  supported for gpt-5.6-sol in /v1/chat/completions." gpt-5.x reasoning models
  reject function tools alongside reasoning in Chat Completions. Fix: the OpenAI
  tool loop now sends `reasoning_effort='none'` (the documented remedy; the other
  option is the /v1/responses API). Configurable via `OPENAI_REASONING_EFFORT`
  (default `none`, threaded through the run/prompt/openai-proxied targets);
  set it to `omit` for non-reasoning models that reject the parameter. Empty
  values fall back to `none`, so an unset Makefile passthrough stays safe.
- Added a mocked OpenAI tool-loop test asserting `reasoning_effort` is sent —
  it reproduces and guards this regression without an API call.

## [Model-callable run_shell tool (native tool-calling)] - 2026-09-02

### Added
- **`ALLOW_SHELL_TOOL=1`** — a second, independent shell knob alongside
  `ALLOW_SHELL`. Where `!exec` is operator-driven, this advertises a `run_shell`
  tool the model calls **autonomously** via native tool-calling. `agent.py`
  runs a bounded model → tool-call → tool-result loop (`_run_tool_turn`) with
  provider-specific implementations: Anthropic `tool_use`/`tool_result` over the
  streaming API, OpenAI `tool_calls`/`tool` role over Chat Completions. Both
  share the `_run_shell` executor with `!exec` (same `SHELL_TIMEOUT`, same
  `/workspace` cwd, same container hardening). Wired into probe + REPL; the env
  block reports `shell_tool_enabled`.
- Knob plumbed through the six run/prompt targets and `capture-logs.sh`;
  documented in `.env.example` and the README "Capabilities & Permissions"
  section (both shell modes explained).
- `make clean-agents` — removes stray agent containers (e.g. after a closed
  terminal); interactive targets use `--rm` so this is only for genuinely stuck
  containers.

### Notes
- **Live-validated (Anthropic):** with `ALLOW_SHELL_TOOL=1`, claude-sonnet-5
  called `run_shell("id")` unprompted → `uid=1000` → produced its final answer.
  End-to-end wire format confirmed against the real API. The OpenAI loop is built
  symmetrically and unit-tested; validate it with `ALLOW_SHELL_TOOL=1 make run-openai`.
- Cross-turn history stores each turn's **final text** (not the intermediate
  tool_use/tool_result blocks), keeping the shared REPL conversation
  provider-neutral; the tool exchange is complete within a single turn.
- Loop is bounded (`max_iters=6`) so a tool-happy model can't spin forever.
- Suite 42 → 48 (executor, schema, gating, and a mocked Anthropic tool-loop
  test that needs no API).

## [Provider-neutral container names] - 2026-09-02

### Changed
- Renamed the images now that the agent is multi-provider: agent
  `claude-agent` → `sandbox-agent` (Makefile `IMAGE_NAME`, `capture-logs.sh`
  default + `sandbox-agent-<session>` container name), proxy `anthropic-proxy`
  → `llm-proxy` (Makefile `PROXY_IMAGE`/`PROXY_NAME`/`PROXY_LOG`, capture-logs
  default + metadata). README Makefile-variable defaults updated. All references
  were variable-driven, so targets pick up the new names automatically; old
  `claude-agent`/`anthropic-proxy` images and any running `anthropic-proxy`
  container are obsolete (remove with `docker rm -f anthropic-proxy` +
  `docker rmi claude-agent:latest anthropic-proxy:latest`).

## [Opt-in agent capabilities: shell exec + container permission knobs] - 2026-09-02

### Added
- **`!exec <cmd>` REPL command** (`agent.py`), operator-driven like `!fetch` and
  gated by `ALLOW_SHELL=1` (default off). Runs the command in the container with
  a `SHELL_TIMEOUT`-second cap (default 30) and truncated output; the model can
  suggest commands but the operator runs them. The probe/REPL env block now
  reports `shell_enabled`, and the system prompt tells the model the operator
  can run `!exec` when it is enabled.
- **Container permission knobs** (Makefile, secure defaults), applied to the
  plain `run`/`prompt` targets and their provider/proxied variants:
  - `WORKSPACE_EXEC=1` — lets the agent run scripts it writes to `/workspace`.
  - `CAP_ADD=<caps>` — comma-separated Linux capabilities.
  - `RUN_AS_ROOT=1` — run as uid 0 (still gVisor-contained).
  - `ALLOW_SHELL` / `SHELL_TIMEOUT` also plumb through `capture-logs.sh`.
- README **"Capabilities & Permissions (for experimenting)"** section documenting
  every knob, verified behavior, and examples.

### Notes (empirically verified against runsc)
- `noexec` is enforced by gVisor: a script in `/workspace` gets `EACCES` on
  `execve`. Dropping the word `noexec` from `--tmpfs` is **not** enough — the
  mount defaults back to `noexec`; `WORKSPACE_EXEC=1` passes the explicit `exec`
  option, which does remove it (mount → `rw,nosuid`, exec succeeds).
- `--cap-add` alone adds a capability to the **bounding** set (`CapBnd`) but a
  non-root agent keeps `CapEff: 0` — the cap is not usable. `RUN_AS_ROOT=1`
  promotes it into the effective set (`CapEff`). So CAP_ADD is paired with
  RUN_AS_ROOT to actually grant a usable capability.
- Renamed the proxy logger `anthropic_proxy` → `llm_proxy` (it serves both
  providers now; the per-request log already tags `provider`).

## [Proxied OpenAI + per-provider make targets] - 2026-09-02

### Added
- **OpenAI traffic can now run network-isolated through the proxy**, matching the
  guarantee the proxy already gave Anthropic. The proxy routes by request path:
  Anthropic paths → `api.anthropic.com` + injected `x-api-key`; OpenAI paths
  (`/v1/chat/completions`, `/v1/responses`) → `api.openai.com` + injected
  `Authorization: Bearer`. One proxy container serves both; `start-proxy` passes
  whichever keys are set. The real key never enters the sandbox for either
  provider. Live-validated: both paths reached their real upstreams (401 on fake
  keys), disallowed paths 403 without forwarding.
- Dedicated per-provider make targets so no env vars need setting by hand:
  `run-openai`, `prompt-openai` (direct) and `run-openai-proxied`,
  `prompt-openai-proxied` (network-isolated). The existing `run`/`prompt`/
  `run-proxied`/`prompt-proxied` remain the Anthropic set.
- `proxy_config.py`: `openai_api_key`, `openai_base_url`
  (`PROXY_OPENAI_UPSTREAM_URL`), `openai_allowed_paths`
  (`PROXY_OPENAI_ALLOWED_PATHS`); validation now requires at least one provider
  key. Agent: `OPENAI_PROXY_URL` pins the OpenAI client base to `<proxy>/v1`;
  proxied-mode detection, `proxy_fetch`, and the REPL system prompt recognize
  either provider's proxy var.

### Notes
- Proxy header injection is case-safe (`_set_header`/`_strip_header`): the OpenAI
  branch strips any client-sent `x-api-key`; the Anthropic branch strips any
  client-sent `Authorization` — neither provider's credential leaks onto the
  other's request.
- Selecting OpenAI inside an Anthropic-proxied context without `OPENAI_PROXY_URL`
  is refused (would try a direct `api.openai.com` call from a network-isolated
  container and fail) — the dedicated `run-openai-proxied` target sets it.
- Suite grew 29 → 37 (OpenAI routing + config + agent proxied-base_url tests).

## [Multi-provider support — OpenAI + provider switch] - 2026-09-02

### Added
- OpenAI provider alongside Anthropic. `agent.py` now resolves a provider and
  model at startup: `LLM_PROVIDER` (`anthropic`|`openai`) is an explicit switch;
  unset, it auto-detects from which key is present; when both keys are set,
  Anthropic wins. Per-provider model defaults `claude-sonnet-5` / `gpt-5.6-sol`,
  overridable via `ANTHROPIC_MODEL` / `OPENAI_MODEL`. Active provider + model are
  reported in the probe's environment block.
- Unified streaming layer (`stream_reply` → `_stream_anthropic` / `_stream_openai`)
  so probe and REPL are provider-agnostic. OpenAI uses Chat Completions with
  streaming; refusals surface via `delta.refusal` or `finish_reason ==
  "content_filter"`, mirroring the Anthropic `stop_reason == "refusal"` notice.
- `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_MAX_TOKENS`, `LLM_PROVIDER` env vars
  threaded through the direct-mode Makefile targets and `capture-logs.sh`;
  `.env.example` and the README document them. `openai` added to the Dockerfile
  and `requirements-dev.txt`.

### Notes
- **Proxied mode stays Anthropic-only** — the proxy injects the Anthropic key and
  forwards to `api.anthropic.com`. `create_client` refuses `openai` + a set
  `ANTHROPIC_PROXY_URL` rather than misrouting.
- The `openai` import is lazy (only when the OpenAI provider is active), so
  Anthropic-only runs and the test suite don't require it at import time.
- `create_client()` keeps its no-arg signature (resolves the provider itself),
  so the existing transport tests are unchanged — suite still 29 green.
- gpt-5.6-sol and its API surface postdate this code: Chat Completions +
  `max_completion_tokens` is the assumed shape (sent only when
  `OPENAI_MAX_TOKENS` is set). Verify against current OpenAI docs if a call
  fails; `MAX_TOKENS = 128000` remains the Anthropic-only output cap.

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
