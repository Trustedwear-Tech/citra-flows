# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
ssrf.py — SSRF guard for outbound HTTP from workflow nodes.

Any node that fetches a user- or AI-supplied URL (API source, webhook
output, the agent HTTP tool) must route the URL through
``assert_url_is_public()`` before making the request, so a workflow can
never reach the cloud metadata endpoint, localhost, or internal/private
hosts.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# Hostnames that must never be reached even if they resolve publicly —
# cloud-metadata aliases.
_BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata",
    "metadata.google.internal",
    "metadata.google.com",
}


def _ip_is_disallowed(ip) -> bool:
    """True for any address class a workflow must not reach.

    ``is_link_local`` covers the AWS/GCP/Azure metadata IP (169.254.169.254)
    and IPv6 fe80::/10; ``is_private`` covers 10/8, 172.16/12, 192.168/16
    and IPv6 ULA fc00::/7.
    """
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def assert_url_is_public(url: str) -> None:
    """Raise ``ValueError`` unless *url* is an http(s) URL whose host
    resolves entirely to public IP addresses.

    Blocks: non-http(s) schemes; localhost / metadata aliases; literal
    private / loopback / link-local / reserved / multicast IPs in ANY
    encoding (decimal / octal / hex / IPv6 — ``ipaddress`` normalises
    them); and hostnames that resolve to any such address.

    Residual: this does not pin the resolved IP, so a DNS-rebinding
    attacker could still flip the record between this check and the
    actual connect. Mitigate by keeping redirect-following disabled at
    every call site; IP pinning is a future hardening step.
    """
    parsed = urlparse(url or "")
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError(
            f"URL scheme '{scheme or '(none)'}' is not allowed — only "
            f"http and https are permitted."
        )

    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise ValueError("URL has no host.")
    if hostname in _BLOCKED_HOSTNAMES:
        raise ValueError("Requests to internal / metadata hosts are not allowed.")

    # Literal IP (any encoding) — validate directly, no DNS needed.
    literal_ip = None
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if _ip_is_disallowed(literal_ip):
            raise ValueError(
                "Requests to internal / private addresses are not allowed."
            )
        return

    # Hostname — resolve all addresses; EVERY one must be public.
    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise ValueError(f"Could not resolve hostname '{hostname}'.")

    for info in addr_infos:
        sockaddr = info[4]
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _ip_is_disallowed(ip):
            raise ValueError(
                "Requests to internal / private addresses are not allowed "
                f"(host '{hostname}' resolves to a non-public address)."
            )
