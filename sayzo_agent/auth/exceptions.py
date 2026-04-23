"""Auth-specific exceptions."""
from __future__ import annotations


class AuthenticationRequired(Exception):
    """No valid credentials — user needs to log in."""


class AuthenticationFailed(Exception):
    """Login attempt failed (timeout, denied, network error)."""


class AuthenticationCancelled(Exception):
    """Login attempt cancelled by the user (e.g., clicked Cancel in the
    setup window while the browser flow was pending).

    Distinct from :class:`AuthenticationFailed` so UI callers can render
    "idle" (not "error") when cancellation was deliberate."""


class PKCEUnavailable(Exception):
    """Localhost redirect not possible — fall back to device code flow."""
