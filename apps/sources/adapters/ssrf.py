"""Server-Side Request Forgery (SSRF) validation utilities.

Blocks access to loopback, private, link-local, carrier-grade NAT,
multicast, and cloud metadata networks.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from apps.sources.adapters.base import SSRFError

# Disallowed IPv4 networks
DISALLOWED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # Carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),  # Loopback
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local / Cloud metadata (AWS/GCP/Azure)
    ipaddress.ip_network("172.16.0.0/12"),  # Private
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("192.88.99.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),  # Private
    ipaddress.ip_network("198.18.0.0/15"),  # Benchmark
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),  # Multicast
    ipaddress.ip_network("240.0.0.0/4"),  # Reserved
]

DISALLOWED_IPV6_NETWORKS = [
    ipaddress.ip_network("::1/128"),  # Loopback
    ipaddress.ip_network("::/128"),  # Unspecified
    ipaddress.ip_network("fc00::/7"),  # Unique local
    ipaddress.ip_network("fe80::/10"),  # Link-local
    ipaddress.ip_network("ff00::/8"),  # Multicast
]


def is_ip_disallowed(ip_str: str) -> bool:
    """Check whether an IP address belongs to a disallowed range."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True

    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved:
        return True

    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in net for net in DISALLOWED_NETWORKS)
    else:
        return any(ip in net for net in DISALLOWED_IPV6_NETWORKS)


def validate_url_for_ssrf(url: str, *, allow_hosts: list[str] | None = None) -> None:
    """Validate a URL against SSRF vulnerabilities.

    Raises SSRFError if the URL scheme is unsupported or points to a private/loopback address.
    """
    if not url or not isinstance(url, str):
        raise SSRFError(f"Invalid URL: {url!r}")

    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise SSRFError(
            f"Scheme '{parsed.scheme}' is not permitted. Only HTTP and HTTPS are allowed."
        )

    hostname = parsed.hostname
    if not hostname:
        raise SSRFError(f"URL missing hostname: {url}")

    hostname_lower = hostname.lower()

    # Explicit allowlist (e.g. for containerized services like rsshub in docker)
    if allow_hosts and any(hostname_lower == h.lower() for h in allow_hosts):
        return

    # Check for direct localhost / loopback aliases
    if hostname_lower in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):  # noqa: S104
        raise SSRFError(f"Access to localhost/loopback is blocked: {hostname}")

    # Resolve IP addresses for hostname
    try:
        addr_info = socket.getaddrinfo(
            hostname, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
        )
    except socket.gaierror as err:
        raise SSRFError(f"Failed to resolve host '{hostname}': {err}") from err

    resolved_ips = {addr[4][0] for addr in addr_info}
    if not resolved_ips:
        raise SSRFError(f"No IP addresses resolved for '{hostname}'")

    for ip in resolved_ips:
        if is_ip_disallowed(ip):
            raise SSRFError(f"Target host '{hostname}' resolved to disallowed IP: {ip}")
