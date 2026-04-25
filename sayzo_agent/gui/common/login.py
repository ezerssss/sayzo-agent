"""Shared PKCE login orchestration for pywebview bridges.

Setup and Settings both expose ``start_login`` / ``cancel_login`` to JS with
identical semantics: spawn a worker thread that drives the PKCE flow, push
``login_url`` / ``login_tick`` / ``login_done`` / ``login_error`` /
``login_cancelled`` events back to the React frontend, and let a second
``start_login`` cancel and supersede the first.

Both bridges previously hand-rolled this logic. The two implementations
drifted slightly (different thread names, different lock variable naming)
even before any real divergence — exactly the maintenance-tax pattern this
extraction prevents.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from sayzo_agent.config import Config

log = logging.getLogger(__name__)


@dataclass
class ActiveLogin:
    """Book-keeping for an in-flight PKCE login.

    Held by the coordinator while a login worker is running so a second
    ``start_login`` can cancel and supersede the first. The ``thread``
    field is informational only — we never join() (would block the JS
    bridge); the worker observes ``cancel_event`` on its next poll.
    """

    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None


class LoginCoordinator:
    """Manages the PKCE login worker lifecycle for a pywebview bridge.

    Constructed once per bridge instance with the bridge's ``push_event``
    callback. Each ``start()`` spawns a fresh worker thread; if a previous
    worker is still running it is signalled to cancel before the new one
    starts. ``cancel()`` is idempotent and safe to call when no worker is
    in flight.
    """

    def __init__(
        self,
        cfg: Config,
        push_event: Callable[[dict[str, Any]], None],
        *,
        thread_name: str = "gui-login",
    ) -> None:
        self._cfg = cfg
        self._push_event = push_event
        self._thread_name = thread_name
        self._active: Optional[ActiveLogin] = None
        self._lock = threading.Lock()

    def start(self) -> ActiveLogin:
        """Start a fresh login worker, cancelling any prior one in flight."""
        with self._lock:
            prior = self._active
            if prior is not None:
                # Don't join — would block the JS bridge call. The worker
                # observes the cancel flag on its next poll (<= 0.5 s) and
                # cleans up.
                prior.cancel_event.set()
            active = ActiveLogin()
            self._active = active

        t = threading.Thread(
            target=self._worker,
            args=(active,),
            name=self._thread_name,
            daemon=True,
        )
        active.thread = t
        t.start()
        return active

    def cancel(self) -> bool:
        """Signal cancellation to the active worker, if any. Returns True iff
        a worker was running and has now been signalled."""
        with self._lock:
            active = self._active
        if active is None:
            return False
        active.cancel_event.set()
        return True

    def _worker(self, active: ActiveLogin) -> None:
        import asyncio

        from sayzo_agent.auth.exceptions import AuthenticationCancelled

        def on_url(url: str) -> None:
            self._push_event({"type": "login_url", "url": url})

        def on_tick(secs: int) -> None:
            self._push_event({"type": "login_tick", "seconds_remaining": secs})

        try:
            # Imported lazily so this module can be imported in tests without
            # dragging in the full CLI module's transitive deps.
            from sayzo_agent.__main__ import _do_login

            asyncio.run(
                _do_login(
                    self._cfg,
                    quiet=True,
                    cancel_event=active.cancel_event,
                    on_url_ready=on_url,
                    on_tick=on_tick,
                )
            )
        except AuthenticationCancelled:
            log.info("[login] cancelled")
            self._push_event({"type": "login_cancelled"})
            return
        except Exception as e:
            log.warning("[login] failed", exc_info=True)
            self._push_event({"type": "login_error", "message": str(e)})
            return
        finally:
            with self._lock:
                if self._active is active:
                    self._active = None
        self._push_event({"type": "login_done"})
