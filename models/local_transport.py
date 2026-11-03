"""Validate and open literal-loopback model requests without proxies or redirects."""

from __future__ import annotations

import ipaddress
import urllib.request
from typing import Any
from urllib.parse import urlsplit


def require_loopback_url(value: str) -> None:
    """Do not resolve DNS; admit only literal loopback HTTP(S) endpoints."""
    try:
        url = urlsplit(value)
        valid = (
            url.scheme in ("http", "https")
            and url.hostname is not None
            and ipaddress.ip_address(url.hostname).is_loopback
            and url.username is None
            and url.password is None
            and not url.query
            and not url.fragment
            and url.port != 0
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("local-only policy requires a literal loopback base URL")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def local_opener() -> urllib.request.OpenerDirector:
    """Ignore proxy environment variables and refuse HTTP redirects."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
