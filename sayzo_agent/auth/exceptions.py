"""Auth-specific exceptions."""
from __future__ import annotations


class AuthenticationRequired(Exception):
    """No valid credentials — user needs to log in."""


class AuthTemporarilyUnavailable(AuthenticationRequired):
    """Auth server unreachable (network/timeout) — NOT a credential problem.

    The existing tokens are almost certainly still valid; the server was
    just unreachable (classically: the agent launched at login before the
    network came up, so the token refresh hit a ``ConnectTimeout``). The
    caller should retry later, NOT prompt re-login.

    Subclasses :class:`AuthenticationRequired` deliberately: every existing
    ``except AuthenticationRequired`` handler keeps catching it, so a
    transient blip degrades to the prior behaviour (no regression, no
    uncaught exception). Callers that care to distinguish a retryable
    network failure — the account + capture pollers — catch THIS
    subclass first and map it to ``transient_error`` (back off + retry)
    instead of ``auth_required``. Before this split a cold-boot
    ConnectTimeout was misreported as "session expired" and the 60s poller
    hammered a full traceback every cycle (see store.py /
    account/status.py)."""


class AuthenticationFailed(Exception):
    """Login attempt failed (timeout, denied, network error)."""


class AuthenticationCancelled(Exception):
    """Login attempt cancelled by the user (e.g., clicked Cancel in the
    setup window while the browser flow was pending).

    Distinct from :class:`AuthenticationFailed` so UI callers can render
    "idle" (not "error") when cancellation was deliberate."""


class PKCEUnavailable(Exception):
    """Localhost redirect not possible — fall back to device code flow."""
