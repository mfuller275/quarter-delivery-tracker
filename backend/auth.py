"""Per-user Microsoft Entra ID authentication (OAuth 2.0 authorization-code flow).

Each browser user signs in with their own Microsoft account (their own MFA) and
receives an Azure DevOps access token scoped to *their* identity. Tokens are kept
in a small server-side session store (keyed by a signed session-id cookie), so we
can silently refresh them without exceeding cookie size limits.

Enable by setting ``AUTH_MODE=oauth`` plus the ``ENTRA_*`` values in ``.env``.
When ``AUTH_MODE=interactive`` (default) none of this is used and the app keeps
its original single shared-identity behaviour.
"""
from __future__ import annotations

import secrets

import msal
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from .ado_client import ADO_SCOPE
from .config import Settings, get_settings

# ADO delegated scope MSAL should request (without the trailing /.default so we
# get a refreshable delegated token rather than an app-only .default token).
_SCOPES = ["499b84ac-1321-427f-aa17-267ca6975798/user_impersonation"]

router = APIRouter()

# In-memory session store: sid -> {"cache": SerializableTokenCache,
# "account": {...}, "flow": {...}}. Fine for a single-process uvicorn deploy
# (the app is run without --reload). For multi-process/replicated hosting,
# replace with a shared store (Redis, a DB, etc.).
_SESSIONS: dict[str, dict] = {}

_SID_KEY = "sid"


def _authority(settings: Settings) -> str:
    return f"https://login.microsoftonline.com/{settings.entra_tenant_id}"


def _load_cache(sid: str) -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    blob = _SESSIONS.get(sid, {}).get("cache")
    if blob:
        cache.deserialize(blob)
    return cache


def _save_cache(sid: str, cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        _SESSIONS.setdefault(sid, {})["cache"] = cache.serialize()


def _msal_app(settings: Settings, cache: msal.SerializableTokenCache | None = None):
    return msal.ConfidentialClientApplication(
        settings.entra_client_id,
        authority=_authority(settings),
        client_credential=settings.entra_client_secret,
        token_cache=cache,
    )


def _get_sid(request: Request, *, create: bool = False) -> str | None:
    sid = request.session.get(_SID_KEY)
    if sid is None and create:
        sid = secrets.token_urlsafe(32)
        request.session[_SID_KEY] = sid
        _SESSIONS[sid] = {}
    return sid


# -- public helpers used by main.py --------------------------------------------
def current_account(request: Request) -> dict | None:
    """Return the signed-in user's account info, or None if not authenticated."""
    sid = request.session.get(_SID_KEY)
    if not sid:
        return None
    return _SESSIONS.get(sid, {}).get("account")


def get_ado_token(request: Request) -> str:
    """Return a fresh ADO access token for the current session's user.

    Raises HTTP 401 if the user is not signed in or the token can't be renewed
    silently (the frontend should then send the user to /auth/login).
    """
    settings = get_settings()
    sid = request.session.get(_SID_KEY)
    account = _SESSIONS.get(sid, {}).get("account") if sid else None
    if not sid or not account:
        raise HTTPException(status_code=401, detail="Not signed in", headers={"X-Auth-Redirect": "/auth/login"})

    cache = _load_cache(sid)
    app = _msal_app(settings, cache)
    accounts = app.get_accounts(username=account.get("username"))
    result = None
    if accounts:
        result = app.acquire_token_silent(_SCOPES, account=accounts[0])
    _save_cache(sid, cache)
    if not result or "access_token" not in result:
        raise HTTPException(
            status_code=401,
            detail="Session expired, please sign in again",
            headers={"X-Auth-Redirect": "/auth/login"},
        )
    return result["access_token"]


# -- routes --------------------------------------------------------------------
@router.get("/auth/login")
def auth_login(request: Request) -> RedirectResponse:
    settings = get_settings()
    if settings.auth_mode != "oauth":
        raise HTTPException(status_code=404, detail="OAuth login is not enabled")
    sid = _get_sid(request, create=True)
    app = _msal_app(settings)
    flow = app.initiate_auth_code_flow(_SCOPES, redirect_uri=settings.oauth_redirect_uri)
    _SESSIONS.setdefault(sid, {})["flow"] = flow
    return RedirectResponse(flow["auth_uri"])


@router.get("/auth/callback")
def auth_callback(request: Request) -> RedirectResponse:
    settings = get_settings()
    if settings.auth_mode != "oauth":
        raise HTTPException(status_code=404, detail="OAuth login is not enabled")
    sid = _get_sid(request)
    flow = _SESSIONS.get(sid, {}).get("flow") if sid else None
    if not sid or not flow:
        raise HTTPException(status_code=400, detail="No auth flow in progress; start at /auth/login")

    cache = _load_cache(sid)
    app = _msal_app(settings, cache)
    # MSAL validates state, nonce and PKCE using the stored flow.
    result = app.acquire_token_by_auth_code_flow(flow, dict(request.query_params))
    if "error" in result:
        raise HTTPException(
            status_code=401,
            detail=f"Sign-in failed: {result.get('error_description', result['error'])}",
        )
    claims = result.get("id_token_claims", {})
    _SESSIONS.setdefault(sid, {})["account"] = {
        "username": claims.get("preferred_username") or claims.get("upn"),
        "name": claims.get("name"),
        "oid": claims.get("oid"),
    }
    _SESSIONS[sid].pop("flow", None)
    _save_cache(sid, cache)
    return RedirectResponse("/")


@router.get("/auth/logout")
def auth_logout(request: Request) -> RedirectResponse:
    sid = request.session.get(_SID_KEY)
    if sid:
        _SESSIONS.pop(sid, None)
    request.session.clear()
    return RedirectResponse("/")


@router.get("/auth/me")
def auth_me(request: Request) -> dict:
    settings = get_settings()
    account = current_account(request)
    return {
        "authMode": settings.auth_mode,
        "authenticated": account is not None,
        "user": account,
    }
