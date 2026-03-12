# gVisor Claude Sandbox PoC

Proof of concept for sandboxing Claude AI agent execution using gVisor (runsc). Runs a Python agent inside a hardened Docker container with dropped capabilities, read-only root filesystem, resource limits, and optional complete network isolation via a Unix socket proxy.

## Quick Start

```bash
# 1. Set your API key
cp .env.example .env
# Edit .env with your real ANTHROPIC_API_KEY

# 2. Build and run
make run
```

## Tech Stack

| Technology | Version | Purpose |
|-----------|---------|---------|
| Python | 3.12 | Agent runtime |
| Anthropic SDK | latest | Claude API client |
| Docker | — | Container runtime |
| gVisor (runsc) | — | Application kernel / sandbox runtime |
| GNU Make | — | Build and run orchestration |

## Project Structure

```
.
├── .env.example                              # Environment template (API key)
├── .gitignore                                # Git ignore rules
├── Dockerfile                                # Python 3.12 slim, non-root user, Claude SDK
├── Makefile                                  # Build/run targets with security hardening
├── README.md                                 # This file
├── agent.py                                  # Claude SDK agent — probes sandbox and queries Claude
└── specs/
    └── network-isolated-sandbox-proxy-spec.md  # FEAT-001: Unix socket proxy spec (Draft)
```

## Available Commands

| Command | Description |
|---------|-------------|
| `make help` | Show all available targets |
| `make build` | Build the agent Docker image |
| `make run` | Build and run agent in gVisor sandbox (API key from env) |
| `make run-proxied` | Run agent with Unix socket proxy (network-isolated, requires proxy — see spec) |
| `make verify-gvisor` | Verify gVisor runtime via dmesg output |
| `make clean` | Remove the agent Docker image |

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes (`run`) | Anthropic API key for Claude access |
| `ANTHROPIC_BASE_URL` | No | Override API endpoint (used by `run-proxied`) |

### Makefile Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `IMAGE_NAME` | `claude-agent` | Docker image name |
| `RUNTIME` | `runsc` | Container runtime (gVisor) |
| `MEMORY` | `2g` | Container memory limit |
| `CPUS` | `2` | CPU limit |
| `PIDS_LIMIT` | `100` | Max processes in container |
| `TMP_SIZE` | `100m` | Writable /tmp size |
| `WORK_SIZE` | `500m` | Writable /workspace size |

## Security Hardening

The `make run` target applies these restrictions:

- **gVisor runtime** — syscall filtering via application kernel
- **`--cap-drop ALL`** — no Linux capabilities
- **`--security-opt no-new-privileges`** — prevent privilege escalation
- **`--read-only`** — immutable root filesystem
- **`--tmpfs`** — writable /tmp and /workspace with noexec, nosuid, size limits
- **Resource limits** — memory, CPU, PID caps
- **Non-root user** — runs as UID 1000

The `run-proxied` target adds **`--network none`** for complete network isolation, routing API calls through a bind-mounted Unix socket.

## How It Works

1. `agent.py` collects runtime environment info (hostname, platform, filesystem permissions, network state)
2. Sends environment data to Claude via the Anthropic SDK
3. Claude analyzes the sandbox restrictions and suggests boundary-testing experiments
4. Output is printed to stdout

## Prerequisites

- Linux host (gVisor only runs on Linux)
- [gVisor (runsc)](https://gvisor.dev/docs/user_guide/install/) installed and configured as a Docker runtime
- Docker installed

## Roadmap

- **FEAT-001:** Network-isolated sandbox proxy — host-side HTTP reverse proxy on a Unix domain socket for `run-proxied` mode (see `specs/network-isolated-sandbox-proxy-spec.md`)
