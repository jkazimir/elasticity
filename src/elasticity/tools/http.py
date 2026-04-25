"""HTTP request tool."""

import ipaddress
import json
import socket
from typing import Any, Dict, Optional
from urllib.parse import urlparse

try:
    import httpx
except ImportError:
    httpx = None

# Module-level flag: when True, block requests to private/loopback/link-local IPs.
_block_private_ips: bool = True


def _tool_init(config: Dict[str, Any]) -> None:
    """Called once by ToolRegistry when this module is first loaded."""
    global _block_private_ips
    if config:
        _block_private_ips = config.get("block_private_ips", True)


def _check_url(url: str) -> None:
    """Raise ValueError if the URL target resolves to a private/internal IP."""
    if not _block_private_ips:
        return
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise ValueError(f"Cannot parse hostname from URL: {url}")
    try:
        addr = socket.getaddrinfo(host, None)[0][4][0]
        ip = ipaddress.ip_address(addr)
    except (socket.gaierror, ValueError):
        # Can't resolve — let the request fail naturally
        return
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        raise PermissionError(
            f"Requests to private/internal addresses are blocked (resolved '{host}' → '{ip}'). "
            "Set block_private_ips: false in the tool config to allow this."
        )


def request(
    url: str,
    method: str = "GET",
    body: Optional[str] = None,
    headers: Optional[str] = None,
) -> str:
    """Make an HTTP request to a URL.

    Args:
        url: URL to request
        method: HTTP method (GET, POST, PUT, DELETE, etc.)
        body: Request body (for POST/PUT)
        headers: JSON string of headers to include

    Returns:
        Response body as a string

    Raises:
        ImportError: If httpx is not installed
        PermissionError: If the URL targets a private/internal IP
        Exception: If the HTTP request fails
    """
    if httpx is None:
        raise ImportError(
            "The 'httpx' package is required for HTTP requests.\n"
            "Install it with: pip install httpx"
        )

    _check_url(url)

    # Parse headers if provided
    headers_dict = {}
    if headers:
        try:
            headers_dict = json.loads(headers)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid headers JSON: {e}") from e

    # Make request
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.request(
                method=method.upper(),
                url=url,
                content=body.encode("utf-8") if body else None,
                headers=headers_dict,
            )
            response.raise_for_status()
            return response.text
    except httpx.HTTPError as e:
        raise Exception(f"HTTP request failed: {e}") from e
