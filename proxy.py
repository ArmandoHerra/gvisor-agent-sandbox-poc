#!/usr/bin/env python3
"""
LLM API Proxy — TCP reverse proxy for Anthropic and/or OpenAI.

Runs on the host, injects the real API key, and forwards requests upstream.
Routing is by request path: Anthropic paths go to https://api.anthropic.com
with an injected x-api-key; OpenAI paths go to https://api.openai.com with an
injected Authorization: Bearer token. External hosts (X-Target-Host) are
forwarded verbatim with no key injection.

Usage:
    python proxy.py                          # Uses defaults + env vars
    ANTHROPIC_API_KEY=sk-ant-... python proxy.py
    OPENAI_API_KEY=sk-...      python proxy.py   # enables OpenAI routing
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from proxy_config import ProxyConfig

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logger = logging.getLogger("llm_proxy")


def setup_logging(config: ProxyConfig) -> None:
    """Configure structured logging."""
    level = getattr(logging, config.log_level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stderr)

    if config.log_format == "json":

        class JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                log_entry = {
                    "timestamp": self.formatTime(record),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                }
                if hasattr(record, "extra_data"):
                    log_entry.update(record.extra_data)
                if record.exc_info and record.exc_info[0] is not None:
                    log_entry["exception"] = self.formatException(record.exc_info)
                return json.dumps(log_entry)

        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )

    logger.addHandler(handler)
    logger.setLevel(level)


# ---------------------------------------------------------------------------
# Token-bucket rate limiter
# ---------------------------------------------------------------------------


class TokenBucketRateLimiter:
    """Simple async-safe token bucket rate limiter."""

    def __init__(self, rate: float, burst: int) -> None:
        """
        Args:
            rate: Tokens added per second (e.g., 1.0 = 60/min).
            burst: Maximum bucket size (burst capacity).
        """
        self._rate = rate
        self._burst = burst
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        """Try to consume one token. Returns True if allowed, False if rate-limited."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            self._last_refill = now

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


# ---------------------------------------------------------------------------
# Header / path helpers
# ---------------------------------------------------------------------------


def _match_paths(path: str, allowed: list[str]) -> bool:
    """True if path exactly matches or is nested under an allowed prefix."""
    return any(path == a or path.startswith(a + "/") for a in allowed)


def _strip_header(headers: dict, name: str) -> None:
    """Remove every case variant of a header from a plain dict."""
    for key in [k for k in headers if k.lower() == name.lower()]:
        del headers[key]


def _set_header(headers: dict, name: str, value: str) -> None:
    """Set a header, first removing any existing case variant."""
    _strip_header(headers, name)
    headers[name] = value


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------


async def handle_request(request: web.Request) -> web.StreamResponse:
    """
    Forward an incoming request to the Anthropic API.

    Steps:
      1. Validate path against allowlist
      2. Check rate limit
      3. Enforce request body size limit
      4. Inject real API key
      5. Forward to upstream
      6. Stream response back
    """
    config: ProxyConfig = request.app["config"]
    rate_limiter: TokenBucketRateLimiter = request.app["rate_limiter"]
    upstream_session: aiohttp.ClientSession = request.app["upstream_session"]
    request_start = time.monotonic()

    # --- 0. External host routing ---
    target_host = request.headers.get("X-Target-Host", "").strip().lower()
    is_external = bool(target_host)

    if is_external:
        if target_host not in config.allowed_external_hosts:
            elapsed_ms = (time.monotonic() - request_start) * 1000
            logger.warning(
                "Blocked request to disallowed external host",
                extra={
                    "extra_data": {
                        "method": request.method,
                        "host": target_host,
                        "path": request.path,
                        "status": 403,
                        "latency_ms": round(elapsed_ms, 2),
                        "reason": "host_not_allowed",
                    }
                },
            )
            return web.json_response(
                {
                    "error": {
                        "type": "forbidden",
                        "message": f"Host '{target_host}' is not in the allowed external hosts list.",
                    }
                },
                status=403,
            )

    # --- 1. Path allowlist + provider routing (external hosts skip this) ---
    request_path = request.path
    provider = None
    if not is_external:
        if _match_paths(request_path, config.allowed_paths):
            provider = "anthropic"
        elif _match_paths(request_path, config.openai_allowed_paths):
            provider = "openai"

        reason = None
        if provider is None:
            reason = "path_not_allowed"
            message = f"Path '{request_path}' is not in the allowed list."
        elif provider == "openai" and not config.openai_api_key:
            reason = "openai_not_configured"
            message = "OpenAI provider is not configured (OPENAI_API_KEY not set on proxy)."

        if reason:
            elapsed_ms = (time.monotonic() - request_start) * 1000
            logger.warning(
                "Blocked request",
                extra={
                    "extra_data": {
                        "method": request.method,
                        "path": request_path,
                        "status": 403,
                        "latency_ms": round(elapsed_ms, 2),
                        "reason": reason,
                    }
                },
            )
            return web.json_response(
                {"error": {"type": "forbidden", "message": message}},
                status=403,
            )

    # --- 2. Rate limiting ---
    if not await rate_limiter.acquire():
        elapsed_ms = (time.monotonic() - request_start) * 1000
        logger.warning(
            "Rate limit exceeded",
            extra={
                "extra_data": {
                    "method": request.method,
                    "path": request_path,
                    "status": 429,
                    "latency_ms": round(elapsed_ms, 2),
                }
            },
        )
        return web.json_response(
            {
                "error": {
                    "type": "rate_limit_error",
                    "message": "Proxy rate limit exceeded. Try again later.",
                }
            },
            status=429,
            headers={"Retry-After": "1"},
        )

    # --- 3. Read and validate body size ---
    body = b""
    if request.can_read_body:
        try:
            body = await request.read()
        except Exception as exc:
            logger.error(f"Failed to read request body: {exc}")
            return web.json_response(
                {"error": {"type": "invalid_request", "message": "Could not read request body."}},
                status=400,
            )
        if len(body) > config.max_request_body_bytes:
            elapsed_ms = (time.monotonic() - request_start) * 1000
            logger.warning(
                "Request body too large",
                extra={
                    "extra_data": {
                        "method": request.method,
                        "path": request_path,
                        "body_bytes": len(body),
                        "max_bytes": config.max_request_body_bytes,
                        "status": 413,
                        "latency_ms": round(elapsed_ms, 2),
                    }
                },
            )
            return web.json_response(
                {
                    "error": {
                        "type": "request_too_large",
                        "message": f"Request body ({len(body)} bytes) exceeds limit ({config.max_request_body_bytes} bytes).",
                    }
                },
                status=413,
            )

    # --- 4. Build upstream request ---
    if is_external:
        upstream_url = f"https://{target_host}{request_path}"
    elif provider == "openai":
        upstream_url = f"{config.openai_base_url}{request_path}"
    else:  # anthropic
        upstream_url = f"{config.upstream_base_url}{request_path}"
    if request.query_string:
        upstream_url += f"?{request.query_string}"

    # Copy headers, strip hop-by-hop and proxy-specific headers
    upstream_headers = {}
    hop_by_hop = {"host", "connection", "transfer-encoding", "keep-alive", "upgrade", "accept-encoding"}
    proxy_headers = {"x-target-host"}
    strip_headers = hop_by_hop | proxy_headers
    for key, value in request.headers.items():
        if key.lower() not in strip_headers:
            upstream_headers[key] = value

    if is_external:
        # External host: no API key injection; Host header is derived from URL by aiohttp
        pass
    elif provider == "openai":
        # OpenAI: inject a Bearer token; strip any client-sent (dummy) key material
        _set_header(upstream_headers, "Authorization", f"Bearer {config.openai_api_key}")
        _strip_header(upstream_headers, "x-api-key")
    else:
        # Anthropic: inject the real API key; strip any client-sent Authorization
        _set_header(upstream_headers, "x-api-key", config.anthropic_api_key)
        _strip_header(upstream_headers, "authorization")
        if not any(k.lower() == "anthropic-version" for k in upstream_headers):
            upstream_headers["anthropic-version"] = "2023-06-01"

    # --- 5. Forward to upstream ---
    try:
        timeout = aiohttp.ClientTimeout(
            connect=config.upstream_connect_timeout,
            sock_read=config.upstream_read_timeout,
            total=None,
        )
        async with upstream_session.request(
            method=request.method,
            url=upstream_url,
            headers=upstream_headers,
            data=body if body else None,
            timeout=timeout,
        ) as upstream_resp:
            # --- 6. Stream response back ---
            content_type = upstream_resp.headers.get("Content-Type", "")
            is_streaming = "text/event-stream" in content_type

            # aiohttp auto-decompresses, so strip encoding/length from response
            strip_resp = hop_by_hop | {"content-encoding", "content-length"}

            if is_streaming:
                response = web.StreamResponse(
                    status=upstream_resp.status,
                    headers={
                        k: v
                        for k, v in upstream_resp.headers.items()
                        if k.lower() not in strip_resp
                    },
                )
                await response.prepare(request)
                async for chunk in upstream_resp.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
            else:
                resp_body = await upstream_resp.read()
                response = web.Response(
                    status=upstream_resp.status,
                    body=resp_body,
                    headers={
                        k: v
                        for k, v in upstream_resp.headers.items()
                        if k.lower() not in strip_resp
                    },
                )

            elapsed_ms = (time.monotonic() - request_start) * 1000
            log_data = {
                "method": request.method,
                "path": request_path,
                "upstream_status": upstream_resp.status,
                "latency_ms": round(elapsed_ms, 2),
                "request_bytes": len(body),
                "streaming": is_streaming,
            }
            if is_external:
                log_data["external_host"] = target_host
            elif provider:
                log_data["provider"] = provider
            logger.info(
                "Proxied request",
                extra={"extra_data": log_data},
            )
            return response

    except asyncio.TimeoutError:
        elapsed_ms = (time.monotonic() - request_start) * 1000
        logger.error(
            "Upstream timeout",
            extra={
                "extra_data": {
                    "method": request.method,
                    "path": request_path,
                    "status": 504,
                    "latency_ms": round(elapsed_ms, 2),
                }
            },
        )
        return web.json_response(
            {"error": {"type": "timeout", "message": "Upstream request timed out."}},
            status=504,
        )
    except aiohttp.ClientError as exc:
        elapsed_ms = (time.monotonic() - request_start) * 1000
        logger.error(
            f"Upstream connection error: {exc}",
            extra={
                "extra_data": {
                    "method": request.method,
                    "path": request_path,
                    "status": 502,
                    "latency_ms": round(elapsed_ms, 2),
                    "error": str(exc),
                }
            },
        )
        return web.json_response(
            {"error": {"type": "upstream_error", "message": "Failed to connect to upstream API."}},
            status=502,
        )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint — always allowed regardless of path allowlist."""
    return web.json_response({"status": "ok", "proxy": "llm-api-proxy"})


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------


async def on_startup(app: web.Application) -> None:
    """Create the shared upstream HTTP session."""
    app["upstream_session"] = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=20),
    )
    cfg = app["config"]
    logger.info(
        "Proxy started",
        extra={
            "extra_data": {
                "listen": f"{cfg.listen_host}:{cfg.listen_port}",
                "anthropic_enabled": bool(cfg.anthropic_api_key),
                "anthropic_upstream": cfg.upstream_base_url,
                "anthropic_allowed_paths": cfg.allowed_paths,
                "openai_enabled": bool(cfg.openai_api_key),
                "openai_upstream": cfg.openai_base_url,
                "openai_allowed_paths": cfg.openai_allowed_paths,
                "rate_limit_rpm": cfg.rate_limit_requests_per_minute,
                "max_body_bytes": cfg.max_request_body_bytes,
            }
        },
    )


async def on_shutdown(app: web.Application) -> None:
    """Close the upstream HTTP session and clean up socket."""
    session: aiohttp.ClientSession = app["upstream_session"]
    await session.close()
    await asyncio.sleep(0.25)
    logger.info("Proxy shut down")


async def on_cleanup(app: web.Application) -> None:
    """Post-shutdown cleanup."""
    logger.info("Proxy cleanup complete")


def create_app(config: ProxyConfig | None = None) -> web.Application:
    """Create and configure the aiohttp application."""
    if config is None:
        config = ProxyConfig.from_env()

    errors = config.validate()
    if errors:
        for err in errors:
            print(f"CONFIG ERROR: {err}", file=sys.stderr)
        sys.exit(1)

    setup_logging(config)

    app = web.Application(client_max_size=config.max_request_body_bytes)
    app["config"] = config
    app["rate_limiter"] = TokenBucketRateLimiter(
        rate=config.rate_limit_requests_per_minute / 60.0,
        burst=config.rate_limit_burst,
    )

    # Routes
    app.router.add_route("GET", "/health", handle_health)
    app.router.add_route("*", "/{path_info:.*}", handle_request)

    # Lifecycle hooks
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.on_cleanup.append(on_cleanup)

    return app


def run_proxy() -> None:
    """Entry point: build app, bind to TCP, run until terminated."""
    config = ProxyConfig.from_env()
    app = create_app(config)

    runner = web.AppRunner(app)

    async def start() -> None:
        await runner.setup()
        site = web.TCPSite(runner, config.listen_host, config.listen_port)
        await site.start()

        print(
            f"Proxy listening on {config.listen_host}:{config.listen_port} "
            f"(upstream: {config.upstream_base_url})",
            file=sys.stderr,
        )

        # Wait for termination signal
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()

        print("\nShutting down proxy...", file=sys.stderr)
        await runner.cleanup()

    asyncio.run(start())


if __name__ == "__main__":
    run_proxy()
