"""Passwords, sessions, CSRF and secret storage.

Deliberately boring: scrypt for passwords (stdlib, memory-hard), opaque server-side session
tokens in an httponly cookie (revocable, unlike a stateless JWT), double-submit CSRF for forms,
and Fernet for merchant API secrets at rest.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session as DBSession, select

from .db import get_session
from .models import Org, Session as SessionRow, User, utcnow

SESSION_COOKIE = "baaki_session"
CSRF_COOKIE = "baaki_csrf"
SESSION_TTL = timedelta(days=14)

# scrypt parameters — ~100ms per hash on a laptop, which is the point.
_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)


# ---- passwords ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, dk_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p), dklen=len(dk_hex) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), dk_hex)


def password_problem(password: str) -> str | None:
    if len(password) < 10:
        return "Password must be at least 10 characters."
    if password.lower() in {"password12", "baaki12345", "1234567890"}:
        return "That password is too common."
    return None


# ---- secrets at rest -----------------------------------------------------------------------
def _fernet() -> Fernet:
    key = os.environ.get("BAAKI_SECRET_KEY")
    if not key:
        # Dev fallback: derived, stable per machine, and clearly not for production.
        key = base64.urlsafe_b64encode(hashlib.sha256(b"baaki-dev-only-key").digest()).decode()
    if len(key) != 44:  # not a Fernet key — derive one so operators can set any passphrase
        key = base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()).decode()
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return None


def mask(value: str | None) -> str:
    if not value:
        return "—"
    return value[:8] + "…" + value[-4:] if len(value) > 14 else "set"


# ---- sessions ------------------------------------------------------------------------------
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: DBSession, user: User, request: Request) -> str:
    token = secrets.token_urlsafe(32)
    row = SessionRow(
        token_hash=_token_hash(token),
        user_id=user.id,
        expires_at=utcnow() + SESSION_TTL,
        user_agent=(request.headers.get("user-agent") or "")[:200],
        ip=(request.client.host if request.client else "")[:64],
    )
    db.add(row)
    user.last_login_at = utcnow()
    db.add(user)
    db.commit()
    return token


def revoke_session(db: DBSession, token: str) -> None:
    row = db.exec(select(SessionRow).where(SessionRow.token_hash == _token_hash(token))).first()
    if row:
        row.revoked = True
        db.add(row)
        db.commit()


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax",
        secure=os.environ.get("BAAKI_ENV") == "production",
        max_age=int(SESSION_TTL.total_seconds()), path="/",
    )


def issue_csrf(response) -> str:
    token = secrets.token_urlsafe(24)
    response.set_cookie(CSRF_COOKIE, token, httponly=False, samesite="lax",
                        secure=os.environ.get("BAAKI_ENV") == "production", path="/")
    return token


# ---- request dependencies --------------------------------------------------------------------
class Principal:
    """The authenticated user plus their org. Every query scopes on `org.id`."""

    def __init__(self, user: User, org: Org):
        self.user, self.org = user, org

    @property
    def org_id(self) -> int:
        return self.org.id


def _load_principal(request: Request, db: DBSession) -> Principal | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = db.exec(select(SessionRow).where(SessionRow.token_hash == _token_hash(token))).first()
    if not row or row.revoked:
        return None
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires < utcnow():
        return None
    user = db.get(User, row.user_id)
    if not user:
        return None
    org = db.get(Org, user.org_id)
    return Principal(user, org) if org else None


def current_principal(request: Request, db: DBSession = Depends(get_session)) -> Principal:
    p = _load_principal(request, db)
    if not p:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in")
    return p


def optional_principal(request: Request, db: DBSession = Depends(get_session)) -> Principal | None:
    return _load_principal(request, db)


def require_csrf(request: Request, csrf_token: str = "") -> None:
    """Double-submit: the form field must match the cookie."""
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie or not csrf_token or not hmac.compare_digest(cookie, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token — reload the page and retry.")
