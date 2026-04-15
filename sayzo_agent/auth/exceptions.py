"""Auth-specific exceptions."""
from __future__ import annotations


class AuthenticationRequired(Exception):
    """No valid credentials — user needs to log in."""


class AuthenticationFailed(Exception):
    """Login attempt failed (timeout, denied, network error)."""


class PKCEUnavailable(Exception):
    """Localhost redirect not possible — fall back to device code flow."""
