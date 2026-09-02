# gVisor Agent Sandbox PoC

Proof of concept for sandboxing AI agent execution using gVisor (runsc). Runs a Python agent inside a hardened Docker container with dropped capabilities, read-only root filesystem, resource limits, and network isolation via a TCP reverse proxy on a Docker internal network.

This is a proof of concept, not hardened production software — read [Security Hardening](#security-hardening) before trusting it with anything sensitive.

**Contents:** [Prerequisites](#prerequisites) · [Docker Runtime Setup](#docker-runtime-setup) · [Quick Start](#quick-start) · [Tech Stack](#tech-stack) · [Project Structure](#project-structure) · [Available Commands](#available-commands) · [Configuration](#configuration) · [Logging](#logging) · [Architecture](#architecture) · [Security Hardening](#security-hardening) · [Agent Modes](#agent-modes) · [Testing](#testing) · [Reverting](#reverting)

## Prerequisites

| Requirement | Why | Quick check |
|---|---|---|
| Linux host | gVisor's application kernel only runs on Linux (not macOS/Windows Docker Desktop VMs) | `uname -s` → `Linux` |
| Docker Engine, installed and running | Builds and runs the agent/proxy containers | `docker info >/dev/null && echo "Docker OK"` |
| GNU Make | Drives every workflow in this repo (`make run`, `make venv`, etc.) | `make --version` |
| gVisor (`runsc`) registered as a Docker runtime | Every container in the Makefile is launched with `--runtime=runsc` | `docker info \| grep -A2 -i runtimes` |
| Python 3.12+ | Only needed for `make venv` / running the pytest suite locally | `python3 --version` |

If Docker, Make, or Python aren't installed, install them via your distro's package manager first. If the `runsc` check doesn't list it yet, that's expected on a fresh machine — the next section walks through registering it.

## Docker Runtime Setup

`make run` (and any target that launches a container) needs `runsc` registered as a Docker runtime first. The steps below are the fast path; see **[docker-runtime.md](docker-runtime.md)** for the full runbook — capturing your machine's baseline before you touch anything, per-distro install notes, verification, and a clean revert.

**Principle: runsc is an *additional* runtime, never the default.** Every container in this repo's Makefile is launched with `--runtime=runsc` explicitly (`RUNTIME := runsc`). Registering runsc as an extra runtime is all that's needed — do **not** set `"default-runtime": "runsc"` in `/etc/docker/daemon.json`. That keeps every other container on your machine running under stock `runc`, completely unaffected, and makes reverting later a one-file change.

```bash
# 1. Install runsc (Debian/Ubuntu shown; see docker-runtime.md for other distros)
sudo apt-get update
sudo apt-get install -y apt-transport-https ca-certificates curl gnupg

ARCH=$(dpkg --print-architecture)
KEY=/usr/share/keyrings/gvisor-archive-keyring.gpg
URL=https://storage.googleapis.com/gvisor/releases
LIST=/etc/apt/sources.list.d/gvisor.list

curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor --yes -o "$KEY"
echo "deb [arch=$ARCH signed-by=$KEY] $URL release main" | sudo tee "$LIST"

sudo apt-get update
sudo apt-get install -y runsc

# 2. Register runsc as an additional Docker runtime
sudo runsc install

# 3. Reload Docker's config — reload registers the runtime without restarting running containers
sudo systemctl reload docker
```

Verify:

```bash
# expect "runsc" listed, "Default Runtime: runc"
docker info | grep -A2 -i runtimes   
# runs dmesg inside the sandbox; look for gVisor's kernel banner
make verify-gvisor
```

Before you modify `/etc/docker/daemon.json`, it's worth capturing your current baseline so revert is mechanical rather than guesswork — see [Step 0 in docker-runtime.md](docker-runtime.md). The full runbook also covers troubleshooting a misconfigured runtime and uninstalling `runsc` entirely.

## Quick Start

With Docker and gVisor set up (previous section), you're ready to run the sandbox:

```bash
# 1. Set your API key
cp .env.example .env
# Edit .env with your real ANTHROPIC_API_KEY

# 2. Build and run (direct mode — API key passed into container)
make run

# 3. Or run network-isolated via proxy
make run-proxied

# 4. Interactive REPL — multi-turn conversation inside sandbox
make prompt                # direct mode
make prompt-proxied        # network-isolated mode
```

## Tech Stack

| Technology | Version | Purpose |
|-----------|---------|---------|
| Python | 3.12 | Agent and proxy runtime |
| Anthropic SDK | latest | Claude API client |
| aiohttp | >=3.9 | Async HTTP proxy server |
| Docker | — | Container runtime |
| gVisor (runsc) | — | Application kernel / sandbox runtime |
| GNU Make | — | Build and run orchestration |
| pytest | — | Unit testing framework |

## Project Structure

```
.
├── .env.example                              # Environment template (API key + proxy + logging settings)
├── .gitignore                                # Git ignore rules
├── Dockerfile                                # Agent image — Python 3.12 slim, non-root user
├── Dockerfile.proxy                          # Proxy image — aiohttp reverse proxy
├── Makefile                                  # Build/run/proxy/logging lifecycle targets
├── README.md                                 # This file
├── agent.py                                  # Agent (Anthropic/OpenAI) — probe mode and interactive REPL
├── changelog.md                              # Development changelog (FEAT-002, bug fixes)
├── docker-runtime.md                         # gVisor runtime apply/revert runbook (setup + rollback)
├── logs/                                     # Session logs (git-ignored, host-side only)
│   └── .gitkeep                              # Keeps the directory tracked
├── proxy.py                                  # TCP reverse proxy — rate limiting, path allowlist, streaming
├── proxy_config.py                           # Proxy configuration with env var overrides and validation
├── requirements-dev.txt                      # Dev/test dependencies (proxy deps + anthropic, httpx2, openai, pytest)
├── requirements-proxy.txt                    # Proxy Python dependencies
├── scripts/
│   ├── capture-logs.sh                       # Session orchestrator — creates log dir, runs container, captures logs
│   └── merge-logs.sh                         # Post-mortem log merger — combines agent/proxy/events into session.log
└── tests/
    ├── __init__.py
    ├── conftest.py                           # Shared pytest fixtures
    ├── test_agent_transport.py               # Agent client creation and network isolation tests
    └── test_proxy.py                         # Proxy config, rate limiter, routing, and handler tests
```

## Available Commands

| Command | Description |
|---------|-------------|
| `make help` | Show all available targets |
| `make build` | Build the agent Docker image |
| `make build-proxy` | Build the proxy Docker image |
| `make run` | Build and run agent in gVisor sandbox (Anthropic, direct) |
| `make run-proxied` | Anthropic — start proxy, run agent network-isolated via proxy |
| `make prompt` | Interactive REPL — Anthropic, direct mode |
| `make prompt-proxied` | Interactive REPL — Anthropic, network-isolated via proxy |
| `make run-openai` | Run agent in gVisor sandbox (OpenAI, direct) |
| `make run-openai-proxied` | OpenAI — start proxy, run agent network-isolated via proxy |
| `make prompt-openai` | Interactive REPL — OpenAI, direct mode |
| `make prompt-openai-proxied` | Interactive REPL — OpenAI, network-isolated via proxy |
| `make start-proxy` | Start the proxy container (bridge + internal network) |
| `make stop-proxy` | Stop the proxy container |
| `make restart-proxy` | Restart the proxy so it picks up a rebuilt image or changed keys/config |
| `make proxy-status` | Check if the proxy container is running |
| `make proxy-logs` | Show proxy container logs (snapshot) |
| `make proxy-logs-follow` | Stream proxy logs live — watch requests during a session (Ctrl-C to stop) |
| `make venv` | Create virtualenv and install dev dependencies (proxy + tests) |
| `make verify-gvisor` | Verify gVisor runtime via dmesg output |
| `make clean` | Remove images, network, and clean up |

### Logged Run Targets

| Command | Description |
|---------|-------------|
| `make run-logged` | Run agent (direct mode) — captures logs to `logs/` |
| `make run-proxied-logged` | Run agent via proxy — captures agent + proxy logs to `logs/` |
| `make prompt-logged` | Interactive REPL (direct mode) with log capture |
| `make prompt-proxied-logged` | Interactive REPL (proxied mode) with log capture |

### Log Review and Maintenance

| Command | Description |
|---------|-------------|
| `make logs-list` | List all captured sessions with status, exit code, and duration |
| `make logs-latest` | Display the most recent session's merged log |
| `make logs-review SESSION=<id>` | Display a specific session's merged log |
| `make logs-events SESSION=<id>` | Show Docker runtime events for a session (OOM, signals, exit codes) |
| `make logs-merge SESSION=<id>` | Regenerate `session.log` for a session (if merge was interrupted) |
| `make logs-clean` | Remove sessions older than `LOG_RETENTION_DAYS` (default: 30 days) |
| `make logs-clean-all` | Remove all session logs (keeps `logs/.gitkeep`) |

## Configuration

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | Yes* | — | Anthropic API key (required for the Anthropic provider and all proxied modes) |
| `ANTHROPIC_MODEL` | No | `claude-sonnet-5` | Anthropic model the agent calls (probe + interactive, both modes) |
| `OPENAI_API_KEY` | Yes* | — | OpenAI API key — enables the OpenAI provider (direct mode only) |
| `OPENAI_MODEL` | No | `gpt-5.6-sol` | OpenAI model the agent calls |
| `OPENAI_MAX_TOKENS` | No | _(model default)_ | Output cap for OpenAI (`max_completion_tokens`); omitted when unset |
| `LLM_PROVIDER` | No | _(auto)_ | Force a provider: `anthropic` or `openai`; unset auto-detects |
| `ANTHROPIC_PROXY_URL` | No | — | Set automatically in Anthropic proxied mode; routes requests through the proxy |
| `OPENAI_PROXY_URL` | No | — | Set automatically in OpenAI proxied mode; routes requests through the proxy |
| `PROXY_HOST` | No | `127.0.0.1` | Proxy listen address |
| `PROXY_PORT` | No | `18080` | Proxy listen port |
| `PROXY_RATE_LIMIT_RPM` | No | `60` | Max requests per minute |
| `PROXY_RATE_LIMIT_BURST` | No | `10` | Burst capacity for rate limiter |
| `PROXY_MAX_BODY_BYTES` | No | `1048576` | Max request body size (1 MiB) |
| `PROXY_LOG_LEVEL` | No | `INFO` | Proxy log level |
| `PROXY_LOG_FORMAT` | No | `json` | Log format (`json` or `text`) |
| `PROXY_ALLOWED_PATHS` | No | `/v1/messages,/v1/complete,/v1/messages/batches` | Comma-separated API path allowlist |
| `PROXY_ALLOWED_EXTERNAL_HOSTS` | No | _(empty)_ | Comma-separated external hosts the agent can reach via proxy (e.g., `github.com,google.com`) |

\* At least one of `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` is required.

### Providers

The agent talks to **Anthropic** (default) or **OpenAI**. Selection order:

1. `LLM_PROVIDER=anthropic|openai` — explicit switch.
2. Otherwise auto-detect from which key is set.
3. If **both** keys are set and `LLM_PROVIDER` is unset, **Anthropic wins** — set `LLM_PROVIDER=openai` to switch.

Dedicated per-provider targets set `LLM_PROVIDER` for you — no env vars to remember:

```bash
# Anthropic (default)                 # OpenAI
make run                              make run-openai
make prompt                           make prompt-openai
make run-proxied                      make run-openai-proxied        # network-isolated
make prompt-proxied                   make prompt-openai-proxied

# Override a model per provider (defaults: claude-sonnet-5 / gpt-5.6-sol)
OPENAI_MODEL=gpt-5.6-terra make run-openai
```

> **Heads-up:** `start-proxy` reuses an already-running proxy container. After rebuilding the proxy image or changing keys/config (e.g. switching from an Anthropic-only proxy to also serving OpenAI), run **`make restart-proxy`** — otherwise a stale proxy will 403 the new provider's paths with *"not in the allowed list."*

Set keys in `.env` (auto-loaded by the Makefile). Both providers can run **network-isolated through the proxy** — the proxy routes by request path (Anthropic paths → `api.anthropic.com` with `x-api-key`; OpenAI paths → `api.openai.com` with `Authorization: Bearer`), so a single proxy container serves both. `make start-proxy` passes whichever keys are set; the sandboxed agent only ever sees a dummy key. Two things to know:

- **The real key stays out of the sandbox in proxied mode** — for OpenAI too, exactly as for Anthropic. The proxy injects it; the agent container gets `OPENAI_API_KEY=proxied`.
- **OpenAI output cap** — `gpt-5.6-sol` and its exact API surface postdate this code, so the OpenAI path uses Chat Completions with `max_completion_tokens` only when `OPENAI_MAX_TOKENS` is set (otherwise the model's own default applies). Verify against current OpenAI docs if a call fails.

### Makefile Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `IMAGE_NAME` | `sandbox-agent` | Agent Docker image name |
| `PROXY_IMAGE` | `llm-proxy:latest` | Proxy Docker image name |
| `RUNTIME` | `runsc` | Container runtime (gVisor) |
| `MEMORY` | `2g` | Container memory limit |
| `CPUS` | `2` | CPU limit |
| `PIDS_LIMIT` | `100` | Max processes in container |
| `TMP_SIZE` | `100m` | Writable /tmp size |
| `WORK_SIZE` | `500m` | Writable /workspace size |
| `PROXY_PORT` | `18080` | Proxy TCP port |

## Logging

The logging system captures all container stdout/stderr, Docker runtime events, and session metadata to disk for post-mortem analysis. It runs entirely on the host using shell pipelines — no changes to Docker images or Python dependencies.

### How It Works

Each `make *-logged` target invokes `scripts/capture-logs.sh`, which:

1. Generates a unique session ID (`YYYYMMDD-HHMMSS-<mode>`)
2. Creates `logs/<session-id>/` with a `metadata.json` (start time, config, host info)
3. Updates the `logs/latest` symlink
4. Starts background collectors: `docker events` (runtime events) and `docker logs --follow` (proxy, in proxied mode)
5. Runs the agent container — output is streamed to both the terminal and `agent.log` in real-time
6. On exit: kills collectors, reads the container exit code, updates `metadata.json`, and merges logs into `session.log`

### Session Directory Layout

```
logs/
+-- 20260312-143022-proxied/
|   +-- metadata.json     # Session ID, mode, timestamps, exit code, host info
|   +-- agent.log         # Agent container stdout + stderr
|   +-- proxy.log         # Proxy container stdout + stderr (proxied mode only)
|   +-- events.log        # Docker runtime events (start/stop/OOM/kill) as JSON lines
|   +-- session.log       # Merged + time-sorted log (all sources combined)
+-- 20260312-143155-direct/
|   +-- metadata.json
|   +-- agent.log
|   +-- events.log        # No proxy.log in direct mode
+-- latest -> 20260312-143022-proxied  # Symlink to most recent session
```

The `logs/` directory is git-ignored (contents are ephemeral). Only `logs/.gitkeep` is tracked.

### Post-Mortem Workflow

```bash
# 1. Run with logging
make run-proxied-logged

# 2. List all sessions
make logs-list

# 3. Review the latest session's merged log
make logs-latest

# 4. Review a specific session
make logs-review SESSION=20260312-143022-proxied

# 5. Check runtime events (OOM kills, signals, exit codes)
make logs-events SESSION=20260312-143022-proxied

# 6. Search across all sessions
grep -r "OOM" logs/*/events.log
grep -r "exit_code" logs/*/metadata.json

# 7. Regenerate session.log if merge was interrupted
make logs-merge SESSION=20260312-143022-proxied

# 8. Clean up old sessions
make logs-clean         # removes sessions older than LOG_RETENTION_DAYS
make logs-clean-all     # removes all session logs
```

### Crash Detection

A session with `"ended_at": null` in `metadata.json` indicates an incomplete session (crash, power loss, or daemon failure). `make logs-list` flags these as `INCOMPLETE`. An exit code of `137` indicates an OOM kill.

### Logging Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_DIR` | `logs` | Base directory for session logs |
| `LOG_RETENTION_DAYS` | `30` | Days before `make logs-clean` removes a session |
| `SANDBOX_LOG_ENABLED` | _(empty)_ | Set to `1` to redirect base `run`/`prompt` targets to logged variants |
| `LOG_TIMESTAMPS` | `1` | Set to `1` to inject ISO 8601 timestamps into agent log lines |

Set these in `.env` or export them before running make:

```bash
LOG_RETENTION_DAYS=7 make run-logged
```

## Architecture

### Direct Mode (`make run`)

The agent container connects directly to `https://api.anthropic.com` with the real API key injected as an environment variable. Network access is unrestricted.

### Proxied Mode (`make run-proxied` / `make run-openai-proxied`)

```
┌─────────────────────────┐       ┌────────────────────┐       ┌──────────────────┐
│  Agent Container        │       │  Proxy Container   │──TCP──│  Anthropic API   │
│  (gVisor sandbox)       │──TCP──│  (bridge + internal│       │  api.anthropic…  │
│  --network=proxy-net    │       │   network)         │──TCP──│  OpenAI API      │
│  No internet access     │       │  Routes by path,   │       │  api.openai.com  │
│                         │       │  injects real key  │       │                  │
└─────────────────────────┘       └────────────────────┘       └──────────────────┘
        proxy-net (internal)              bridge (external)
```

1. `start-proxy` creates a Docker internal network (`proxy-net`) and launches the proxy container connected to both `bridge` (internet) and `proxy-net`. It receives whichever real keys are set (`ANTHROPIC_API_KEY` and/or `OPENAI_API_KEY`).
2. The agent container joins only `proxy-net` — no direct internet access
3. The agent sends requests to `http://proxy-host:<port>` with a dummy API key
4. The proxy **routes by request path**: Anthropic paths (`/v1/messages`, …) go to `https://api.anthropic.com` with an injected `x-api-key`; OpenAI paths (`/v1/chat/completions`, `/v1/responses`) go to `https://api.openai.com` with an injected `Authorization: Bearer`. It validates the path, enforces rate limits and body size on both.
5. Responses (including streaming) are relayed back to the agent

### External Host Routing

The proxy can optionally forward requests to whitelisted external hosts, allowing the sandboxed agent to reach trusted services (e.g., GitHub, Google) without direct internet access.

```bash
# In .env, add the hosts you want to allow:
PROXY_ALLOWED_EXTERNAL_HOSTS=github.com,google.com,api.example.com

# Then run as usual — the proxy picks up the setting from .env
make run-proxied
```

The agent sends requests to the proxy with an `X-Target-Host` header:

```python
import httpx

# Route through the proxy to an external host
resp = httpx.get(
    "http://proxy-host:18080/api/repos",
    headers={"X-Target-Host": "github.com"},
)
```

- Only hosts listed in `PROXY_ALLOWED_EXTERNAL_HOSTS` are permitted (403 otherwise)
- External requests skip the Anthropic path allowlist (any path is valid)
- Rate limiting and body size limits still apply
- No API key injection — the `X-Target-Host` header is stripped before forwarding; `Host` is derived from the URL by aiohttp
- All external requests are logged with the target host for audit

### Proxy Security Controls

- **Path allowlist** — Only `/v1/messages`, `/v1/complete`, `/v1/messages/batches` are forwarded (Anthropic API only)
- **External host whitelist** — Optional list of trusted external hosts the agent can reach via `X-Target-Host` header
- **Rate limiting** — Token bucket algorithm (default: 60 RPM, burst 10)
- **Body size limit** — Rejects requests exceeding 1 MiB
- **Key injection** — The real API key never enters the sandbox container (Anthropic requests only)
- **Structured logging** — JSON-formatted request logs for audit

## Security Hardening

The `make run` target applies these restrictions:

- **gVisor runtime** — syscall filtering via application kernel
- **`--cap-drop ALL`** — no Linux capabilities
- **`--security-opt no-new-privileges`** — prevent privilege escalation
- **`--read-only`** — immutable root filesystem
- **`--tmpfs`** — writable /tmp and /workspace with noexec, nosuid, size limits
- **Resource limits** — memory, CPU, PID caps
- **Non-root user** — runs as UID 1000

The `run-proxied` target adds **network isolation** — the agent container can only reach the proxy on the internal Docker network, with no direct internet access.

## Capabilities & Permissions (for experimenting)

The defaults above are locked down. This is a sandbox PoC, so the point is to *relax specific permissions and watch what happens* — but explicitly, one knob at a time, keeping everything else hardened. Permissions live at two layers:

**1. Application layer — giving the agent a shell.** By default the agent has no way to run commands; its only local tool is `!fetch`. There are two independent ways to grant shell access — enable either or both:

- **`ALLOW_SHELL=1` — operator-driven `!exec`.** You type `!exec <cmd>` in the REPL; the command runs and the output is fed back to the model. The model can *suggest* commands but does not run them. Deterministic, human-in-the-loop.
- **`ALLOW_SHELL_TOOL=1` — model-callable `run_shell` tool.** The model is given a `run_shell` tool and decides to call it *autonomously* (native tool-calling for both providers); the agent runs each call and returns the result until the model produces a final answer. This is the "real autonomous agent" mode.

```bash
# Operator-driven
ALLOW_SHELL=1 make prompt           # then: you> !exec id

# Model-driven (the model runs commands itself)
ALLOW_SHELL_TOOL=1 make prompt      # then just ask: you> what uid are you running as?

# Both at once
ALLOW_SHELL=1 ALLOW_SHELL_TOOL=1 make prompt
```

Both share the same executor: commands run in `/workspace` with a `SHELL_TIMEOUT`-second cap (default 30) and truncated output, and what they can *do* is bounded by the container knobs below. (Live-checked: with `ALLOW_SHELL_TOOL=1`, the model called `run_shell` → `id` → `uid=1000` → answered, unprompted.)

**2. Container layer — what the shell is allowed to do.** Opt-in Makefile toggles, each defaulting to the secure value:

| Knob | Default | Effect | Verified behavior |
|------|---------|--------|-------------------|
| `ALLOW_SHELL` | off | Enables the operator `!exec` REPL command | — |
| `ALLOW_SHELL_TOOL` | off | Enables the model-callable `run_shell` tool (native tool-calling) | Model invokes it on its own; loop runs until it stops calling tools |
| `SHELL_TIMEOUT` | `30` | Per-command timeout (seconds), shared by both | — |
| `WORKSPACE_EXEC` | `0` | Lets the agent run scripts it writes to `/workspace` | Passes the explicit `exec` mount option — dropping `noexec` alone is **not** enough (gVisor tmpfs defaults to `noexec`) |
| `CAP_ADD` | _(none)_ | Comma-separated Linux capabilities (e.g. `net_bind_service`) | Adds them to the **bounding** set. Not *effective* for the non-root agent on its own |
| `RUN_AS_ROOT` | `0` | Runs the agent as uid 0 | Makes `CAP_ADD` capabilities **effective** (`CapEff`). Root here is still contained by gVisor's Sentry — it is not host root |

Examples:

```bash
# Let the agent write and execute its own scripts in /workspace
ALLOW_SHELL=1 WORKSPACE_EXEC=1 make prompt
# then: !exec sh -c 'echo "echo hi" > s.sh && chmod +x s.sh && ./s.sh'

# Grant a capability and make it usable (needs root to be effective)
ALLOW_SHELL=1 CAP_ADD=net_bind_service RUN_AS_ROOT=1 make prompt
# then: !exec python3 -c "import socket; s=socket.socket(); s.bind(('0.0.0.0',80)); print('bound :80')"

# Knobs compose with any provider/proxied target and can go in .env instead
ALLOW_SHELL=1 make run-openai-proxied
```

Two facts worth internalizing (both empirically checked against `runsc`):

- **`noexec` is enforced.** A script written to `/workspace` gets `EACCES` on `execve` unless `WORKSPACE_EXEC=1` supplies the explicit `exec` option.
- **A capability is only usable when it is *effective*.** `--cap-add` alone lands the cap in the bounding set (`CapBnd`) but a non-root process keeps `CapEff: 0`; running as root (`RUN_AS_ROOT=1`) promotes it into `CapEff`.

These knobs apply to the plain `run`/`prompt` targets and their provider/proxied variants. The `*-logged` capture variants carry `ALLOW_SHELL`/`SHELL_TIMEOUT` but not `WORKSPACE_EXEC`/`CAP_ADD`/`RUN_AS_ROOT` (they build their own container flags). Each relaxation weakens isolation deliberately — reach for them to probe boundaries, not as defaults.

## Agent Modes

### Probe Mode (default)

1. `agent.py` collects runtime environment info (hostname, platform, filesystem permissions, network state, proxy mode)
2. If `ANTHROPIC_PROXY_URL` is set, the client routes through the proxy; otherwise connects directly
3. Sends environment data to the model via the active provider's SDK
4. The model analyzes the sandbox restrictions and suggests boundary-testing experiments
5. Output is printed to stdout

### Interactive Mode (`--interactive`)

1. Same environment collection and client setup as probe mode
2. Opens a multi-turn REPL with `you>` / `model>` prompts
3. The model receives the sandbox environment as a system prompt and maintains conversation history
4. In proxied mode, use `!fetch <url>` to make HTTP GET requests through the proxy to whitelisted external hosts
5. With `ALLOW_SHELL=1`, use `!exec <cmd>` to run a shell command in the sandbox (see [Capabilities & Permissions](#capabilities--permissions-for-experimenting))
6. Exit with `Ctrl+D`, `exit`, or `quit`

## Testing

### Unit tests

```bash
# Create virtualenv with dependencies
make venv

# Run all tests
.venv/bin/python -m pytest tests/ -v

# Run proxy tests only
.venv/bin/python -m pytest tests/test_proxy.py -v

# Run agent transport tests only
.venv/bin/python -m pytest tests/test_agent_transport.py -v
```

Tests cover:
- Proxy configuration loading and validation
- Token bucket rate limiter behavior
- Health check endpoint
- Path allowlist enforcement
- Request body size limits
- Upstream request forwarding and streaming
- External host routing (allowlist, blocking, case-insensitive matching)
- Agent client creation (direct and proxied modes)
- `proxy_fetch()` and `!fetch` REPL command handling
- Network isolation detection

### Validating the sandbox itself

Unit tests exercise the proxy and agent transport logic in isolation — they don't prove gVisor is actually intercepting the container's syscalls. To validate the sandbox end-to-end:

```bash
# Confirm the container is really running under gVisor (checks dmesg for gVisor's kernel banner)
make verify-gvisor

# Run the agent for real — probe mode reports what the sandbox restricts
make run
```

## Reverting

Done experimenting? Here's how to cleanly remove the PoC from your machine.

```bash
# 1. Stop the proxy container, remove built images and the proxy-net network
make clean
```

Then undo the Docker runtime registration. The exact commands depend on whether you had a `/etc/docker/daemon.json` before you started — full details, including the optional `runsc` uninstall, are in **[docker-runtime.md's Revert section](docker-runtime.md#revert-return-to-your-baseline)**. In short:

```bash
# No daemon.json existed before setup:
sudo rm /etc/docker/daemon.json
# A daemon.json existed and you backed it up before setup:
# sudo mv /etc/docker/daemon.json.pre-gvisor.bak /etc/docker/daemon.json

sudo systemctl restart docker
```

**Note:** unlike the `reload` used during setup, this `restart` stops all running containers unless you have live-restore enabled — time it accordingly.
