"""Shared access control for DashPi and DashESP.

Owns session login, CSRF on mutations, optional login throttling, and a
durable Flask secret key. Device-specific auth (e.g. DashESP's ESP32 shared
key) plugs in via ``pre_auth`` so this module stays free of product-specific
endpoints.

Both products import this file as-is. DashESP pulls it through
``webapp/scripts/sync_dashpi_core.sh``; do not maintain a second copy under
``adapters/``.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol
from urllib.parse import urlsplit

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

logger = logging.getLogger(__name__)

# Default public surface shared by both products. DashPi adds its Wi-Fi
# captive-portal endpoints at install time; DashESP keeps this set.
DEFAULT_PUBLIC_ENDPOINTS = frozenset(
    {
        "auth.login",
        "auth.setup_password",
        "static",
        "main.display_page",
        "main.get_current_image",
    }
)

DASHPI_WIFI_PUBLIC_ENDPOINTS = frozenset(
    {
        "wifi.wifi_portal",
        "wifi.wifi_scan",
        "wifi.wifi_connect",
        "wifi.wifi_status",
        "wifi.captive_android",
        "wifi.captive_apple",
        "wifi.captive_windows",
    }
)

SECRET_KEY_ENV_VAR = "FLASK_SECRET_KEY"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Failures tolerated before lockouts start, then 30s doubling up to 5 minutes.
FREE_LOGIN_ATTEMPTS = 5
BASE_LOCKOUT_SECONDS = 30.0
MAX_LOCKOUT_SECONDS = 300.0

# Returned by ``pre_auth`` to mean "this request is already authorized; skip
# the rest of the gate" (e.g. a device endpoint that checked its own key).
PRE_AUTH_ALLOW = object()


def resolve_secret_key(
    secret_path: str | Path | None = None,
    *,
    env_var: str = SECRET_KEY_ENV_VAR,
) -> str:
    """Return the Flask secret key, generating and persisting one if needed.

    Preference order: environment variable, then the file at ``secret_path``,
    then a freshly generated value written to ``secret_path`` (when provided).

    A hardcoded default would let anyone who has read the repository forge a
    session cookie, so there is deliberately no fallback constant.

    The previous DashPi entry point called ``os.urandom(24).hex()`` on every
    ``serve()``, which signed everyone out on restart. Passing a durable
    ``secret_path`` (or setting the env var) is the fix.
    """
    from_env = os.getenv(env_var, "").strip()
    if from_env:
        return from_env

    path = Path(secret_path) if secret_path is not None else None
    if path is not None:
        try:
            existing = path.read_text(encoding="utf-8").strip()
        except OSError:
            existing = ""
        if existing:
            return existing

    generated = secrets.token_hex(32)
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(generated, encoding="utf-8")
            path.chmod(0o600)
            logger.info("Generated a new session secret at %s", path)
        except OSError as error:
            # Still safe, just not durable: everyone gets logged out on restart.
            logger.warning(
                "Could not persist the session secret (%s); sessions will not "
                "survive a restart. Set %s to fix this.",
                error,
                env_var,
            )
    else:
        logger.warning(
            "No secret_path provided and %s is unset; the session secret will "
            "change every process start.",
            env_var,
        )
    return generated


def csrf_origin_ok(origin: str | None, referer: str | None, host: str) -> bool:
    """Check that a state-changing request came from our own pages.

    Only the host is compared, not the scheme: behind a TLS-terminating reverse
    proxy the browser reports ``https`` while Flask still sees ``http``, and a
    scheme comparison would then reject every mutation. The host is what the
    browser resolved, so it is the part that actually establishes same-origin.
    """
    if origin:
        # A literal "null" origin (sandboxed iframe, some redirects) is not ours.
        return origin != "null" and urlsplit(origin).netloc == host
    if referer:
        return urlsplit(referer).netloc == host
    # Browsers always send Origin on fetch/form POSTs, so neither header
    # present means the request did not come from a page we served.
    return False


class LoginThrottle:
    """Per-IP exponential backoff for failed logins.

    In-process only, which is the right scope here: the app runs as a single
    process and a restart clearing the counters is not a meaningful bypass.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}

    def retry_after(self, address: str) -> float:
        """Seconds the caller must wait, or 0 when it may try now."""
        with self._lock:
            remaining = self._locked_until.get(address, 0.0) - time.monotonic()
            return max(0.0, remaining)

    def record_failure(self, address: str) -> None:
        with self._lock:
            self._prune()
            failures = self._failures.get(address, 0) + 1
            self._failures[address] = failures
            if failures >= FREE_LOGIN_ATTEMPTS:
                backoff = BASE_LOCKOUT_SECONDS * 2 ** (failures - FREE_LOGIN_ATTEMPTS)
                self._locked_until[address] = time.monotonic() + min(
                    backoff, MAX_LOCKOUT_SECONDS
                )

    def record_success(self, address: str) -> None:
        with self._lock:
            self._failures.pop(address, None)
            self._locked_until.pop(address, None)

    def _prune(self) -> None:
        """Drop expired lockouts so the maps cannot grow without bound."""
        now = time.monotonic()
        expired = [
            address
            for address, until in self._locked_until.items()
            if until + MAX_LOCKOUT_SECONDS < now
        ]
        for address in expired:
            self._locked_until.pop(address, None)
            self._failures.pop(address, None)


class PasswordSource(Protocol):
    def has_password(self) -> bool: ...


def install_access_control(
    app: Flask,
    config: PasswordSource,
    *,
    public_endpoints: Iterable[str] | None = None,
    secret_path: str | Path | None = None,
    pre_auth: Callable[[], Any] | None = None,
) -> None:
    """Gate every request: optional pre-auth, CSRF, then session login.

    Parameters
    ----------
    public_endpoints:
        Endpoint names reachable without a session. Defaults to the shared
        set (login, setup, static, display). Matching is by endpoint name,
        never by path prefix — ``/api-keys`` also starts with ``/api``.
    secret_path:
        When provided (and ``app.secret_key`` is still unset), resolve and
        assign a durable signing key via :func:`resolve_secret_key`.
    pre_auth:
        Optional early hook, called before CSRF and session checks. Return:

        * :data:`PRE_AUTH_ALLOW` — request is authorized; skip the rest of
          the gate (used by DashESP for ``/api/esp32/*`` after its key check).
        * a Flask response — short-circuit with that response (e.g. 401).
        * ``None`` — continue with CSRF + session checks.
    """
    if secret_path is not None and not app.secret_key:
        app.secret_key = resolve_secret_key(secret_path)

    allowed = frozenset(public_endpoints) if public_endpoints is not None else DEFAULT_PUBLIC_ENDPOINTS
    throttle = LoginThrottle()

    def wants_json() -> bool:
        return request.path.startswith("/api/") or (
            request.accept_mimetypes.accept_json
            and not request.accept_mimetypes.accept_html
        )

    def unauthenticated_response(error: str, endpoint: str, **values: Any) -> Any:
        """JSON for API callers, a redirect for browsers.

        Always redirecting makes browser fetch() calls silently receive an
        HTML login page instead of an error body.
        """
        if wants_json():
            return jsonify({"ok": False, "error": error}), 401
        return redirect(url_for(endpoint, **values))

    @app.before_request
    def enforce_access_control() -> Any:
        # 1. Product-specific early auth (device keys, etc.).
        if pre_auth is not None:
            early = pre_auth()
            if early is PRE_AUTH_ALLOW:
                return None
            if early is not None:
                return early

        # 2. CSRF: a mutation has to have come from a page we served.
        if request.method in MUTATING_METHODS and not csrf_origin_ok(
            request.headers.get("Origin"),
            request.headers.get("Referer"),
            request.host,
        ):
            logger.warning(
                "CSRF blocked remote=%s path=%s origin=%r referer=%r",
                request.remote_addr,
                request.path,
                request.headers.get("Origin"),
                request.headers.get("Referer"),
            )
            return jsonify({"ok": False, "error": "csrf_validation_failed"}), 403

        # 3. Back off repeated password guesses before the handler sees them.
        if request.endpoint == "auth.login" and request.method == "POST":
            wait = throttle.retry_after(request.remote_addr or "unknown")
            if wait > 0:
                seconds = int(wait) + 1
                logger.warning(
                    "Login throttled remote=%s retry_after=%ss",
                    request.remote_addr,
                    seconds,
                )
                return (
                    render_template(
                        "login.html",
                        error=f"Too many attempts. Try again in {seconds} seconds.",
                    ),
                    429,
                    {"Retry-After": str(seconds)},
                )

        if not request.endpoint or request.endpoint in allowed:
            return None

        # 4. First run: force the admin password to be chosen before anything
        #    else becomes reachable.
        if not config.has_password():
            return unauthenticated_response("password_not_set", "auth.setup_password")

        if not session.get("authenticated"):
            return unauthenticated_response(
                "unauthorized", "auth.login", next=request.url
            )
        return None

    @app.after_request
    def track_login_outcome(response: Any) -> Any:
        """Feed the throttle from the login handler's own verdict.

        auth.login redirects on success and re-renders the form on failure, so
        the status code is enough — no need to patch the auth blueprint.
        """
        if request.endpoint == "auth.login" and request.method == "POST":
            address = request.remote_addr or "unknown"
            if response.status_code == 302:
                throttle.record_success(address)
            elif response.status_code == 200:
                throttle.record_failure(address)
                logger.warning("Failed login remote=%s", address)
        return response
