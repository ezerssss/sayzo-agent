"""Shared test helpers for constructing httpx error fixtures."""
from __future__ import annotations

import json

import httpx


def make_http_error(status: int, body: str | dict = "") -> httpx.HTTPStatusError:
    """Build a real httpx.HTTPStatusError with an attached response body.

    Pass a dict to automatically serialize it as JSON with the correct
    content-type header; pass a string for a raw text body.
    """
    if isinstance(body, dict):
        content = json.dumps(body).encode("utf-8")
        headers = {"content-type": "application/json"}
    else:
        content = body.encode("utf-8") if isinstance(body, str) else body
        headers = {}
    req = httpx.Request("POST", "https://example.com/api/captures/upload")
    resp = httpx.Response(status, content=content, headers=headers, request=req)
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)
