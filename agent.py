#!/usr/bin/env python3
"""Claude SDK agent running inside gVisor sandbox."""

import os
import platform
import socket

import anthropic


def _check_network_isolated() -> bool:
    """Test if we can reach the public internet (not just the proxy)."""
    try:
        s = socket.create_connection(("8.8.8.8", 53), timeout=2)
        s.close()
        return False
    except (OSError, socket.timeout):
        return True


def gather_env_info():
    """Collect runtime environment details to send as context."""
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "user": os.environ.get("USER", os.getuid()),
        "workdir": os.getcwd(),
        "pid": os.getpid(),
        "writable_tmp": os.access("/tmp", os.W_OK),
        "writable_root": os.access("/", os.W_OK),
        "proxied_mode": bool(os.environ.get("ANTHROPIC_PROXY_URL")),
        "network_isolated": _check_network_isolated(),
    }


def create_client() -> anthropic.Anthropic:
    """
    Create an Anthropic client.

    If ANTHROPIC_PROXY_URL is set, route requests through the host-side
    proxy via TCP. The proxy injects the real API key and forwards to
    the upstream Anthropic API.

    If not set, fall back to standard direct connection.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY is not set")
        raise SystemExit(1)

    proxy_url = os.environ.get("ANTHROPIC_PROXY_URL")

    if proxy_url:
        # Proxied mode — route through host-side proxy via TCP
        client = anthropic.Anthropic(
            api_key=api_key,
            base_url=proxy_url,
        )
    else:
        # Direct mode — connect to Anthropic API directly
        client = anthropic.Anthropic(
            api_key=api_key,
            base_url=os.environ.get(
                "ANTHROPIC_BASE_URL", "https://api.anthropic.com"
            ),
        )

    return client


def main():
    env_info = gather_env_info()
    print("=== Container Environment ===")
    for key, value in env_info.items():
        print(f"  {key}: {value}")
    print()

    client = create_client()

    prompt = f"""You are running inside a gVisor-sandboxed Docker container. Here is proof — the runtime environment data collected from inside your container:

{chr(10).join(f'- {k}: {v}' for k, v in env_info.items())}

Based on this environment data, do the following:
1. Confirm you understand you're running in a sandboxed container and mention the hostname and platform.
2. Identify which security restrictions are active (read-only root, dropped capabilities, limited PIDs, etc.).
3. Suggest one creative experiment to test the sandbox boundaries (without actually breaking anything).

Keep your response concise — under 200 words."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    print("=== Claude Response ===")
    print(message.content[0].text)


if __name__ == "__main__":
    main()
