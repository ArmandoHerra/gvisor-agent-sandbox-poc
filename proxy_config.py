#!/usr/bin/env python3
"""Configuration for the Anthropic API proxy."""

import os
from dataclasses import dataclass, field


@dataclass
class ProxyConfig:
    """Proxy configuration with env var overrides."""

    # Listen settings (TCP — gVisor blocks Unix sockets on bind mounts)
    listen_host: str = "0.0.0.0"
    listen_port: int = 18080

    # Anthropic upstream settings
    upstream_base_url: str = "https://api.anthropic.com"
    anthropic_api_key: str = ""

    # OpenAI upstream settings (optional — routing enabled when the key is set)
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com"
    openai_allowed_paths: list[str] = field(default_factory=lambda: [
        "/v1/chat/completions",
        "/v1/responses",
    ])

    # Security settings — Anthropic path allowlist
    allowed_paths: list[str] = field(default_factory=lambda: [
        "/v1/messages",
        "/v1/complete",
        "/v1/messages/batches",
    ])
    allowed_external_hosts: list[str] = field(default_factory=list)
    max_request_body_bytes: int = 1_048_576  # 1 MiB
    rate_limit_requests_per_minute: int = 60
    rate_limit_burst: int = 10

    # Timeouts (seconds)
    upstream_connect_timeout: float = 10.0
    upstream_read_timeout: float = 900.0  # 15 min for long streaming responses

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"  # "json" or "text"

    @classmethod
    def from_env(cls) -> "ProxyConfig":
        """Build config from environment variables."""
        config = cls()
        config.listen_host = os.environ.get(
            "PROXY_HOST", config.listen_host
        )
        config.listen_port = int(
            os.environ.get("PROXY_PORT", str(config.listen_port))
        )
        config.upstream_base_url = os.environ.get(
            "PROXY_UPSTREAM_URL", config.upstream_base_url
        )
        config.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        config.openai_api_key = os.environ.get("OPENAI_API_KEY", "")
        config.openai_base_url = os.environ.get(
            "PROXY_OPENAI_UPSTREAM_URL", config.openai_base_url
        )
        config.max_request_body_bytes = int(
            os.environ.get("PROXY_MAX_BODY_BYTES", str(config.max_request_body_bytes))
        )
        config.rate_limit_requests_per_minute = int(
            os.environ.get(
                "PROXY_RATE_LIMIT_RPM",
                str(config.rate_limit_requests_per_minute),
            )
        )
        config.rate_limit_burst = int(
            os.environ.get("PROXY_RATE_LIMIT_BURST", str(config.rate_limit_burst))
        )
        config.upstream_connect_timeout = float(
            os.environ.get(
                "PROXY_UPSTREAM_CONNECT_TIMEOUT",
                str(config.upstream_connect_timeout),
            )
        )
        config.upstream_read_timeout = float(
            os.environ.get(
                "PROXY_UPSTREAM_READ_TIMEOUT",
                str(config.upstream_read_timeout),
            )
        )
        config.log_level = os.environ.get("PROXY_LOG_LEVEL", config.log_level)
        config.log_format = os.environ.get("PROXY_LOG_FORMAT", config.log_format)

        allowed = os.environ.get("PROXY_ALLOWED_PATHS")
        if allowed:
            config.allowed_paths = [p.strip() for p in allowed.split(",") if p.strip()]

        external_hosts = os.environ.get("PROXY_ALLOWED_EXTERNAL_HOSTS")
        if external_hosts:
            config.allowed_external_hosts = [
                h.strip().lower() for h in external_hosts.split(",") if h.strip()
            ]

        openai_allowed = os.environ.get("PROXY_OPENAI_ALLOWED_PATHS")
        if openai_allowed:
            config.openai_allowed_paths = [
                p.strip() for p in openai_allowed.split(",") if p.strip()
            ]

        return config

    def validate(self) -> list[str]:
        """Return a list of validation errors. Empty list means valid."""
        errors = []
        if not self.anthropic_api_key and not self.openai_api_key:
            errors.append(
                "At least one of ANTHROPIC_API_KEY / OPENAI_API_KEY is required"
            )
        if not self.listen_host:
            errors.append("listen_host is required")
        if not (1 <= self.listen_port <= 65535):
            errors.append("listen_port must be between 1 and 65535")
        if self.max_request_body_bytes <= 0:
            errors.append("max_request_body_bytes must be positive")
        if self.rate_limit_requests_per_minute <= 0:
            errors.append("rate_limit_requests_per_minute must be positive")
        if not self.allowed_paths:
            errors.append("allowed_paths must contain at least one path")
        return errors
