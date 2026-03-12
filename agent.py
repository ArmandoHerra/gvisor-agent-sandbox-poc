#!/usr/bin/env python3
"""Claude SDK agent running inside gVisor sandbox."""

import os
import platform
import socket
import sys
import urllib.error
import urllib.request

import anthropic
import httpx


def _check_network_isolated() -> bool:
    """Test if we can reach the public internet (not just the proxy)."""
    try:
        s = socket.create_connection(("8.8.8.8", 53), timeout=2)
        s.close()
        return False
    except (OSError, socket.timeout):
        return True


def proxy_fetch(url: str, *, proxy_url: str | None = None) -> dict:
    """
    Fetch a URL through the proxy using the X-Target-Host header.

    Parses the target URL, routes the request through the proxy,
    and returns a dict with status, headers, and body.
    """
    proxy_url = proxy_url or os.environ.get("ANTHROPIC_PROXY_URL")
    if not proxy_url:
        return {"error": "No proxy URL configured (ANTHROPIC_PROXY_URL not set)"}

    # Parse the target URL to extract host and path
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"

    if not host:
        return {"error": f"Could not parse host from URL: {url}"}

    # Route through proxy with X-Target-Host header
    proxy_request_url = f"{proxy_url.rstrip('/')}{path}"
    req = urllib.request.Request(
        proxy_request_url,
        headers={"X-Target-Host": host, "User-Agent": "gvisor-sandbox-agent/1.0"},
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
            return {
                "status": resp.status,
                "url": url,
                "content_length": len(body),
                "content_type": resp.headers.get("Content-Type", ""),
                "body_preview": body[:500].decode("utf-8", errors="replace"),
            }
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        return {"status": e.code, "url": url, "error": error_body}
    except Exception as e:
        return {"url": url, "error": str(e)}


def gather_env_info():
    """Collect runtime environment details to send as context."""
    proxy_url = os.environ.get("ANTHROPIC_PROXY_URL")
    info = {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "user": os.environ.get("USER", os.getuid()),
        "workdir": os.getcwd(),
        "pid": os.getpid(),
        "writable_tmp": os.access("/tmp", os.W_OK),
        "writable_root": os.access("/", os.W_OK),
        "proxied_mode": bool(proxy_url),
        "network_isolated": _check_network_isolated(),
    }
    if proxy_url:
        info["proxy_url"] = proxy_url
    return info


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


def print_env(env_info: dict) -> None:
    """Print container environment info."""
    print("=== Container Environment ===")
    for key, value in env_info.items():
        print(f"  {key}: {value}")
    print()


def run_probe(client: anthropic.Anthropic, env_info: dict) -> None:
    """Run the default sandbox probe prompt."""
    prompt = f"""You are running inside a gVisor-sandboxed Docker container. Here is proof — the runtime environment data collected from inside your container:

{chr(10).join(f'- {k}: {v}' for k, v in env_info.items())}

Based on this environment data, do the following:
1. Confirm you understand you're running in a sandboxed container and mention the hostname and platform.
2. Identify which security restrictions are active (read-only root, dropped capabilities, limited PIDs, etc.).
3. Suggest one creative experiment to test the sandbox boundaries (without actually breaking anything).

Keep your response concise — under 200 words."""

    print("=== Claude Response ===")
    try:
        with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for text in stream.text_stream:
                print(text, end="", flush=True)
    except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
        print(f"\n\n[stream interrupted: {exc}]")
    print()


def _handle_fetch_command(cmd: str) -> str | None:
    """Handle !fetch <url> commands. Returns output string or None if not a fetch command."""
    if not cmd.startswith("!fetch "):
        return None

    url = cmd[7:].strip()
    if not url:
        return "[fetch] Usage: !fetch <url>  (e.g., !fetch https://github.com)"

    # Add https:// if no scheme provided
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    result = proxy_fetch(url)

    if "error" in result:
        status = result.get("status", "N/A")
        return f"[fetch] {url}\n  Status: {status}\n  Error: {result['error']}"

    lines = [
        f"[fetch] {url}",
        f"  Status: {result['status']}",
        f"  Content-Type: {result['content_type']}",
        f"  Content-Length: {result['content_length']} bytes",
        f"  Preview: {result['body_preview'][:200]}...",
    ]
    return "\n".join(lines)


def run_interactive(client: anthropic.Anthropic, env_info: dict) -> None:
    """Interactive REPL — multi-turn conversation with Claude inside the sandbox."""
    env_context = "\n".join(f"- {k}: {v}" for k, v in env_info.items())
    proxy_url = os.environ.get("ANTHROPIC_PROXY_URL")

    system_prompt = (
        "You are running inside a gVisor-sandboxed Docker container. "
        "Here is the runtime environment:\n\n"
        f"{env_context}\n\n"
        "Answer the user's questions. You are aware of your sandboxed context.\n\n"
    )

    if proxy_url:
        system_prompt += (
            "IMPORTANT: This container is network-isolated. Direct HTTP requests "
            "and DNS lookups will fail. However, the user has a special REPL command "
            "'!fetch <url>' that routes HTTP GET requests through the proxy to "
            "whitelisted external hosts. Only hosts configured in the proxy's "
            "PROXY_ALLOWED_EXTERNAL_HOSTS will succeed; others return 403. "
            "When the user asks to fetch a URL or test connectivity, suggest they "
            "use '!fetch <url>' (e.g., '!fetch https://google.com'). "
            "Direct socket connections, ping, and DNS will always fail in this sandbox."
        )

    messages: list[dict] = []
    print("Interactive mode — type your prompts below. Ctrl+D or 'exit' to quit.")
    if proxy_url:
        print("  Use '!fetch <url>' to make HTTP requests through the proxy.")
    print()

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("Bye.")
            break

        # Handle !fetch commands locally (no Claude roundtrip needed)
        fetch_output = _handle_fetch_command(user_input)
        if fetch_output is not None:
            print(f"\n{fetch_output}\n")
            # Add fetch result to conversation so Claude has context
            messages.append({"role": "user", "content": user_input})
            messages.append({"role": "assistant", "content": fetch_output})
            continue

        messages.append({"role": "user", "content": user_input})

        print("\nclaude> ", end="", flush=True)
        reply_parts: list[str] = []
        try:
            with client.messages.stream(
                model="claude-opus-4-6",
                max_tokens=32768,  # max for claude-opus-4-6 (sonnet-4-6 max is 16384)
                system=system_prompt,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    print(text, end="", flush=True)
                    reply_parts.append(text)
        except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
            print(f"\n\n[stream interrupted: {exc}]")
        print("\n")

        reply = "".join(reply_parts)
        if reply:
            messages.append({"role": "assistant", "content": reply})
        else:
            # Remove the user message if we got no response at all
            messages.pop()


def main():
    interactive = "--interactive" in sys.argv

    env_info = gather_env_info()
    print_env(env_info)

    client = create_client()

    if interactive:
        run_interactive(client, env_info)
    else:
        run_probe(client, env_info)


if __name__ == "__main__":
    main()
