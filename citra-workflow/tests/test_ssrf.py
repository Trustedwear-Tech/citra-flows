"""Tests for the SSRF guard — utils/ssrf.py (H4).

All cases use literal IPs / bad schemes / blocked hostnames, so no real
DNS lookup happens — the suite is hermetic.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from citra_workflow.utils.ssrf import assert_url_is_public  # noqa: E402


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata (IMDS)
    "http://127.0.0.1:8080",                      # loopback
    "http://127.5.5.5",                           # full 127/8 loopback range
    "http://10.0.0.1",                            # private 10/8
    "http://192.168.1.1",                         # private 192.168/16
    "http://172.16.0.1",                          # private 172.16/12
    "http://localhost/admin",                     # blocked hostname
    "http://metadata.google.internal/x",          # blocked metadata alias
    "http://0.0.0.0",                             # unspecified
    "http://[::1]/x",                             # IPv6 loopback
    "http://2130706433/",                         # decimal-encoded 127.0.0.1
    "file:///etc/passwd",                         # non-http(s) scheme
    "ftp://example.com",                          # non-http(s) scheme
    "https:///nopath",                            # no host
])
def test_blocked_urls_raise(url):
    with pytest.raises(ValueError):
        assert_url_is_public(url)


@pytest.mark.parametrize("url", [
    "https://8.8.8.8/webhook",       # public literal IP — no DNS
    "http://93.184.216.34:8080/x",   # public literal IP
])
def test_public_urls_allowed(url):
    # Should not raise.
    assert_url_is_public(url)
