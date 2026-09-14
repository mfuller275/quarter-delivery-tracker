"""Device-code sign-in manager for the shared-identity modes.

Lets the FIRST sign-in (and re-sign-in after token expiry) happen from any
browser instead of only the server console: the device code is surfaced through
the web UI. A background thread runs the (blocking) device-code flow while the
prompt (URL + code) is exposed via status; API requests meanwhile only ever use
an already-acquired/silently-refreshed token, so they never block on a prompt.

Still a single shared identity: whoever completes the device login, the app acts
as that account for all viewers (matches AUTH_MODE=interactive/devicecode).
"""
from __future__ import annotations

import threading
import time

from azure.identity import DeviceCodeCredential, TokenCachePersistenceOptions

from .ado_client import ADO_SCOPE
from .config import Settings


class _SilentAuthRequired(Exception):
    """Raised inside the prompt callback to abort a silent-only attempt before
    any user interaction is needed."""


class DeviceAuthManager:
    def __init__(self, cache_name: str = "quarter_delivery_tracker", timeout: int = 600):
        self._lock = threading.Lock()
        self._cred_lock = threading.Lock()
        self._allow_prompt = False
        self._prompt: dict | None = None
        self._status = "idle"  # idle | starting | pending | authenticated | error
        self._error: str | None = None
        self._thread: threading.Thread | None = None
        self._token: str | None = None
        self._expires_on: float = 0.0
        self._credential = DeviceCodeCredential(
            timeout=timeout,
            prompt_callback=self._on_prompt,
            cache_persistence_options=TokenCachePersistenceOptions(name=cache_name),
        )

    # -- prompt callback -------------------------------------------------
    def _on_prompt(self, verification_uri: str, user_code: str, expires_on) -> None:
        if not self._allow_prompt:
            raise _SilentAuthRequired()
        self._prompt = {
            "verificationUri": verification_uri,
            "userCode": user_code,
            "message": (
                f"To sign in, open {verification_uri} on any device and enter the "
                f"code {user_code}."
            ),
        }
        self._status = "pending"

    # -- token access ----------------------------------------------------
    def _valid(self) -> bool:
        return bool(self._token) and (self._expires_on - 120) > time.time()

    def get_token_silent(self) -> str | None:
        """Return a valid token if one is cached or can be refreshed silently,
        else None. Never triggers an interactive prompt or blocks on one."""
        if self._valid():
            return self._token
        if self._status in ("starting", "pending"):
            return None
        if not self._cred_lock.acquire(blocking=False):
            return None
        try:
            self._allow_prompt = False
            try:
                tok = self._credential.get_token(ADO_SCOPE)
            except Exception:
                return None
            self._token = tok.token
            self._expires_on = tok.expires_on
            self._status = "authenticated"
            return self._token
        finally:
            self._cred_lock.release()

    # -- interactive flow ------------------------------------------------
    def _run_flow(self) -> None:
        with self._cred_lock:
            self._allow_prompt = True
            try:
                tok = self._credential.get_token(ADO_SCOPE)
                self._token = tok.token
                self._expires_on = tok.expires_on
                self._status = "authenticated"
                self._prompt = None
            except Exception as exc:  # noqa: BLE001 - report to the UI
                self._status = "error"
                self._error = str(exc)
            finally:
                self._allow_prompt = False

    def start_device_login(self) -> dict:
        """Kick off (or reuse) a device-code flow and return the current status,
        including the prompt (URL + code) once available."""
        if self._valid():
            return {"status": "authenticated"}
        with self._lock:
            if not (self._thread and self._thread.is_alive()):
                self._status = "starting"
                self._prompt = None
                self._error = None
                self._thread = threading.Thread(target=self._run_flow, daemon=True)
                self._thread.start()
        # Wait briefly for the code (or a fast silent success/failure).
        deadline = time.time() + 8
        while time.time() < deadline:
            if self._prompt or self._status in ("authenticated", "error"):
                break
            time.sleep(0.15)
        return self.status()

    def status(self) -> dict:
        out: dict = {"status": self._status}
        if self._prompt and self._status == "pending":
            out["prompt"] = self._prompt
        if self._error and self._status == "error":
            out["error"] = self._error
        return out


_manager: DeviceAuthManager | None = None


def get_device_auth_manager(settings: Settings | None = None) -> DeviceAuthManager:
    global _manager
    if _manager is None:
        _manager = DeviceAuthManager()
    return _manager
