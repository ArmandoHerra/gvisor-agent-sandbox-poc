# gVisor Claude Sandbox PoC

Proof of concept for sandboxing Claude AI agent execution using gVisor (runsc). Runs a Python agent inside a hardened Docker container with dropped capabilities, read-only root filesystem, resource limits, and network isolation via a TCP reverse proxy on a Docker internal network.

## Quick Start

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
├── agent.py                                  # Claude SDK agent — probe mode and interactive REPL
├── changelog.md                              # Development changelog (FEAT-002, bug fixes)
├── logs/                                     # Session logs (git-ignored, host-side only)
│   └── .gitkeep                              # Keeps the directory tracked
├── proxy.py                                  # TCP reverse proxy — rate limiting, path allowlist, streaming
├── proxy_config.py                           # Proxy configuration with env var overrides and validation
├── requirements-proxy.txt                    # Proxy Python dependencies
├── scripts/
│   ├── capture-logs.sh                       # Session orchestrator — creates log dir, runs container, captures logs
│   └── merge-logs.sh                         # Post-mortem log merger — combines agent/proxy/events into session.log
├── specs/
│   ├── container-sandbox-logging-capture-spec.md # FEAT-002: Logging & capture spec
│   └── network-isolated-sandbox-proxy-spec.md  # FEAT-001: Proxy implementation spec
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
| `make run` | Build and run agent in gVisor sandbox (API key from env) |
| `make run-proxied` | Build agent + start proxy, run agent network-isolated via proxy |
| `make prompt` | Interactive REPL — direct mode (API key from env) |
| `make prompt-proxied` | Interactive REPL — network-isolated via proxy |
| `make start-proxy` | Start the proxy container (bridge + internal network) |
| `make stop-proxy` | Stop the proxy container |
| `make proxy-status` | Check if the proxy container is running |
| `make proxy-logs` | Show proxy container logs |
| `make venv` | Create virtualenv and install proxy dependencies locally |
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
| `ANTHROPIC_API_KEY` | Yes | — | Anthropic API key for Claude access |
| `ANTHROPIC_PROXY_URL` | No | — | Set automatically in proxied mode; routes requests through proxy |
| `PROXY_HOST` | No | `127.0.0.1` | Proxy listen address |
| `PROXY_PORT` | No | `18080` | Proxy listen port |
| `PROXY_RATE_LIMIT_RPM` | No | `60` | Max requests per minute |
| `PROXY_RATE_LIMIT_BURST` | No | `10` | Burst capacity for rate limiter |
| `PROXY_MAX_BODY_BYTES` | No | `1048576` | Max request body size (1 MiB) |
| `PROXY_LOG_LEVEL` | No | `INFO` | Proxy log level |
| `PROXY_LOG_FORMAT` | No | `json` | Log format (`json` or `text`) |
| `PROXY_ALLOWED_PATHS` | No | `/v1/messages,/v1/complete,/v1/messages/batches` | Comma-separated API path allowlist |
| `PROXY_ALLOWED_EXTERNAL_HOSTS` | No | _(empty)_ | Comma-separated external hosts the agent can reach via proxy (e.g., `github.com,google.com`) |

### Makefile Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `IMAGE_NAME` | `claude-agent` | Agent Docker image name |
| `PROXY_IMAGE` | `anthropic-proxy:latest` | Proxy Docker image name |
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

### Proxied Mode (`make run-proxied`)

```
┌─────────────────────────┐       ┌────────────────────┐       ┌──────────────────┐
│  Agent Container        │       │  Proxy Container   │       │  Anthropic API   │
│  (gVisor sandbox)       │──TCP──│  (bridge + internal│──TCP──│  api.anthropic.  │
│  --network=proxy-net    │       │   network)         │       │  com             │
│  No internet access     │       │  Injects real key  │       │                  │
└─────────────────────────┘       └────────────────────┘       └──────────────────┘
        proxy-net (internal)              bridge (external)
```

1. `start-proxy` creates a Docker internal network (`proxy-net`) and launches the proxy container connected to both `bridge` (internet) and `proxy-net`
2. The agent container joins only `proxy-net` — no direct internet access
3. The agent sends requests to `http://proxy-host:<port>` with a dummy API key
4. The proxy validates the path, enforces rate limits and body size, injects the real API key, and forwards to `https://api.anthropic.com`
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

## How It Works

### Probe Mode (default)

1. `agent.py` collects runtime environment info (hostname, platform, filesystem permissions, network state, proxy mode)
2. If `ANTHROPIC_PROXY_URL` is set, the client routes through the proxy; otherwise connects directly
3. Sends environment data to Claude via the Anthropic SDK
4. Claude analyzes the sandbox restrictions and suggests boundary-testing experiments
5. Output is printed to stdout

### Interactive Mode (`--interactive`)

1. Same environment collection and client setup as probe mode
2. Opens a multi-turn REPL with `you>` / `claude>` prompts
3. Claude receives the sandbox environment as a system prompt and maintains conversation history
4. In proxied mode, use `!fetch <url>` to make HTTP GET requests through the proxy to whitelisted external hosts
5. Exit with `Ctrl+D`, `exit`, or `quit`

## Testing

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

## Prerequisites

- Linux host (gVisor only runs on Linux)
- [gVisor (runsc)](https://gvisor.dev/docs/user_guide/install/) installed and configured as a Docker runtime
- Docker installed
- Python 3.12+ (for local proxy development/testing)
