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
├── .env.example                              # Environment template (API key + proxy settings)
├── .gitignore                                # Git ignore rules
├── Dockerfile                                # Agent image — Python 3.12 slim, non-root user
├── Dockerfile.proxy                          # Proxy image — aiohttp reverse proxy
├── Makefile                                  # Build/run/proxy lifecycle targets
├── README.md                                 # This file
├── agent.py                                  # Claude SDK agent — probe mode and interactive REPL
├── proxy.py                                  # TCP reverse proxy — rate limiting, path allowlist, streaming
├── proxy_config.py                           # Proxy configuration with env var overrides and validation
├── requirements-proxy.txt                    # Proxy Python dependencies
├── specs/
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

### Proxy Security Controls

- **Path allowlist** — Only `/v1/messages`, `/v1/complete`, `/v1/messages/batches` are forwarded
- **Rate limiting** — Token bucket algorithm (default: 60 RPM, burst 10)
- **Body size limit** — Rejects requests exceeding 1 MiB
- **Key injection** — The real API key never enters the sandbox container
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
4. Exit with `Ctrl+D`, `exit`, or `quit`

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
- Agent client creation (direct and proxied modes)
- Network isolation detection

## Prerequisites

- Linux host (gVisor only runs on Linux)
- [gVisor (runsc)](https://gvisor.dev/docs/user_guide/install/) installed and configured as a Docker runtime
- Docker installed
- Python 3.12+ (for local proxy development/testing)
