# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
Authentication for the workflow engine — delegated to Citra-User-Service.

This engine does not own identity. Accounts, passwords, orgs, departments and
roles live in **Citra-User-Service**, which is a separate running service (not a
library), and this module is the thin seam between the two:

    POST /api/auth/login    email + password  -> proxied upstream, returns its token
    GET  /api/auth/me       Bearer token      -> the caller's claims

Why proxy rather than have the browser call the identity service directly: the
UI keeps one origin, so there is no second base URL to configure and no CORS
rules to maintain on the identity service for every front end that talks to it.

**The token is issued upstream and verified here.** Both sides are HS256 over
`JWT_SECRET`, so this service never sees a password and never mints a human
token — it verifies what the identity service signed. That only works if both
processes share the same secret, which is checked explicitly on every login (see
`_verify_upstream_token`) rather than left to fail later as a confusing 401 on
the next request.

The upstream token already carries everything the router reads off
`request.state`: `user_id`, `email`, `org_id`, `dept_ids`, `roles`. Nothing has
to be mapped or re-signed.

There is no local user store, no password handling and no registration here. To
add a user, change a password, or grant `IT-workflow`, use Citra-User-Service.

THE TRADE: this engine's login is only as available as the identity service. If
it is down, existing tokens keep working until they expire, but nobody new can
sign in.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# The identity service. No localhost default: on a container network the right
# value is a service name, and a default that silently points at the wrong place
# produces "Incorrect email or password" for a correct password — the most
# expensive kind of wrong.
USER_SERVICE_URL = (os.getenv("USER_SERVICE_URL") or "").rstrip("/")

# Its email/password endpoint. The service also offers Google and per-customer
# OIDC on sibling paths; this engine deliberately exposes only the local one.
_UPSTREAM_LOGIN_PATH = "/api/auth/local/login"

_LOGIN_TIMEOUT_SECONDS = float(os.getenv("USER_SERVICE_TIMEOUT_SECONDS", "15"))


def _jwt_secret() -> str:
    secret = os.getenv("JWT_SECRET")
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT_SECRET is not set — cannot verify tokens from the identity service.",
        )
    return secret


def _require_user_service() -> str:
    if not USER_SERVICE_URL:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "USER_SERVICE_URL is not set. This engine does not store accounts; "
                "sign-in is delegated to Citra-User-Service."
            ),
        )
    return USER_SERVICE_URL


def _verify_upstream_token(token: str) -> Dict[str, Any]:
    """Verify the token the identity service just issued, and return its claims.

    This is not ceremony. If the two services disagree about `JWT_SECRET`, login
    "succeeds" and then every authenticated request 401s — a failure that looks
    like a broken engine rather than a misconfigured secret. Verifying here turns
    that into one explicit message at the point of the mistake.
    """
    try:
        return jwt.decode(
            token,
            _jwt_secret(),
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except jwt.PyJWTError as exc:
        logger.error(
            "identity service issued a token this service cannot verify (%s) — "
            "JWT_SECRET almost certainly differs between the two",
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "Signed in upstream, but this service cannot verify the token it "
                "issued. JWT_SECRET must be identical in Citra-User-Service and here."
            ),
        ) from exc


# ── Request/response models ───────────────────────────────────────────

class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)


class LoginResponse(BaseModel):
    token: str
    user: Dict[str, Any]
    expires_in: int


# ── Routes ────────────────────────────────────────────────────────────

@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest):
    """Exchange email + password for a bearer token, upstream."""
    base = _require_user_service()

    try:
        async with httpx.AsyncClient(timeout=_LOGIN_TIMEOUT_SECONDS) as client:
            upstream = await client.post(
                f"{base}{_UPSTREAM_LOGIN_PATH}",
                json={"email": body.email.strip().lower(), "password": body.password},
            )
    except httpx.RequestError as exc:
        # Reachability is an operational fault, not a credentials fault. Saying
        # "incorrect password" here would send someone hunting the wrong problem.
        logger.error("identity service unreachable at %s: %s", base, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The identity service is unreachable. Sign-in is unavailable.",
        ) from exc

    if upstream.status_code >= 400:
        # Pass the upstream verdict through rather than inventing one, so a
        # disabled account or an unverified email reads correctly instead of
        # collapsing into "wrong password".
        try:
            detail = upstream.json().get("error") or "Sign-in failed."
        except ValueError:
            detail = "Sign-in failed."
        raise HTTPException(status_code=upstream.status_code, detail=detail)

    payload = upstream.json()
    data = payload.get("data") or {}
    token = data.get("token")
    if not token:
        logger.error("identity service returned 2xx with no token: %s", str(payload)[:300])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The identity service returned no token.",
        )

    claims = _verify_upstream_token(token)

    user = data.get("user") or {}
    # Prefer the claims for the fields authorization actually turns on: the token
    # is what every later request is judged by, so the session should show the
    # same thing rather than a user document that may differ.
    user = {
        **user,
        "user_id": claims.get("user_id"),
        "email": claims.get("email") or user.get("email"),
        "org_id": claims.get("org_id"),
        "dept_ids": claims.get("dept_ids") or [],
        "roles": claims.get("roles") or [],
    }

    exp = int(claims.get("exp") or 0)
    iat = int(claims.get("iat") or 0)
    expires_in = max(0, exp - iat) if exp and iat else 0

    logger.info("🔑 login (upstream): %s", user.get("email"))
    return LoginResponse(token=token, user=user, expires_in=expires_in)


@router.get("/me")
async def me(request: Request):
    """Return the caller's identity, read from the verified token.

    No lookup: the middleware has already verified the signature and expiry, and
    the claims are the same ones every authorization check in this service uses.
    Querying the identity service again would add a network hop per page load and
    could disagree with the token actually in play.
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    return {
        "user": {
            "user_id": user_id,
            # The middleware stores this as `user_email`; there is no
            # `request.state.email` anywhere in the codebase.
            "email": getattr(request.state, "user_email", "") or "",
            "name": getattr(request.state, "user_name", "") or "",
            "org_id": getattr(request.state, "org_id", "") or "",
            "dept_ids": list(getattr(request.state, "dept_ids", []) or []),
            "roles": list(getattr(request.state, "roles", []) or []),
        }
    }
