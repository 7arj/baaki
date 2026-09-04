"""Account lifecycle: sign-in throttling, email verification, password reset and team invites.

Tokens are single-use, expiring, and stored only as a SHA-256 hash — a database leak hands over
no live links. Throttling state lives in the database so limits hold across multiple workers.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlmodel import Session as DBSession, delete, select

from .models import LoginAttempt, Org, Role, Token, TokenPurpose, User, utcnow

# Throttling: a short window with a hard ceiling, applied per email and per IP. Generous enough
# that a real person fumbling their password never notices, tight enough to stop online guessing.
WINDOW = timedelta(minutes=15)
MAX_PER_EMAIL = 6
MAX_PER_IP = 20

TTL = {
    TokenPurpose.VERIFY_EMAIL: timedelta(days=3),
    TokenPurpose.PASSWORD_RESET: timedelta(hours=1),
    TokenPurpose.INVITE: timedelta(days=7),
}


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# ---- throttling ------------------------------------------------------------------------------
def _count(db: DBSession, key: str) -> int:
    since = utcnow() - WINDOW
    return len(db.exec(select(LoginAttempt).where(LoginAttempt.key == key, LoginAttempt.at >= since)).all())


def throttle_problem(db: DBSession, email: str, ip: str) -> str | None:
    """Returns a message if this sign-in should be refused before the password is even checked."""
    if _count(db, f"email:{email.lower()}") >= MAX_PER_EMAIL:
        return "Too many failed sign-in attempts for this account. Try again in 15 minutes, or reset your password."
    if ip and _count(db, f"ip:{ip}") >= MAX_PER_IP:
        return "Too many failed sign-in attempts from this network. Try again in 15 minutes."
    return None


def record_failure(db: DBSession, email: str, ip: str) -> None:
    db.add(LoginAttempt(key=f"email:{email.lower()}"))
    if ip:
        db.add(LoginAttempt(key=f"ip:{ip}"))
    db.commit()


def clear_failures(db: DBSession, email: str, ip: str) -> None:
    db.exec(delete(LoginAttempt).where(LoginAttempt.key == f"email:{email.lower()}"))
    if ip:
        db.exec(delete(LoginAttempt).where(LoginAttempt.key == f"ip:{ip}"))
    db.commit()


def prune_attempts(db: DBSession) -> int:
    """Drop attempts outside the window so the table doesn't grow without bound."""
    old = db.exec(select(LoginAttempt).where(LoginAttempt.at < utcnow() - WINDOW)).all()
    for row in old:
        db.delete(row)
    db.commit()
    return len(old)


# ---- tokens ----------------------------------------------------------------------------------
def issue_token(db: DBSession, purpose: TokenPurpose, *, org_id: int | None = None,
                user_id: int | None = None, email: str = "", role: Role = Role.MEMBER,
                created_by: int | None = None) -> str:
    """Returns the raw token — the only time it exists in plaintext. Supersedes any prior one."""
    for old in db.exec(select(Token).where(Token.purpose == purpose, Token.used_at.is_(None),
                                           Token.user_id == user_id if user_id else Token.email == email.lower())).all():
        old.used_at = utcnow()
        db.add(old)
    raw = secrets.token_urlsafe(32)
    db.add(Token(token_hash=token_hash(raw), purpose=purpose, org_id=org_id, user_id=user_id,
                 email=email.lower(), role=role, created_by=created_by,
                 expires_at=utcnow() + TTL[purpose]))
    db.commit()
    return raw


def consume_token(db: DBSession, raw: str, purpose: TokenPurpose) -> Token | None:
    """Validates and burns a token. Returns None for unknown, wrong-purpose, used or expired."""
    row = db.exec(select(Token).where(Token.token_hash == token_hash(raw))).first()
    if not row or row.purpose != purpose or row.used_at is not None:
        return None
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=utcnow().tzinfo)
    if expires < utcnow():
        return None
    row.used_at = utcnow()
    db.add(row)
    db.commit()
    return row


def peek_token(db: DBSession, raw: str, purpose: TokenPurpose) -> Token | None:
    """Read a token without burning it — for rendering the form before it's submitted."""
    row = db.exec(select(Token).where(Token.token_hash == token_hash(raw))).first()
    if not row or row.purpose != purpose or row.used_at is not None:
        return None
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=utcnow().tzinfo)
    return None if expires < utcnow() else row


# ---- emails ----------------------------------------------------------------------------------
def base_url() -> str:
    import os

    return os.environ.get("BAAKI_BASE_URL", "http://127.0.0.1:8080").rstrip("/")


def send_account_email(to: str, subject: str, body: str) -> None:
    """Account mail goes out immediately rather than through the Outbox.

    The Outbox is the *customer* contact queue — it is policy-gated, approval-gated and subject
    to contact windows. A password reset is none of those things and must not be held behind a
    merchant's approval settings.
    """
    from .transports import build_transport

    build_transport().send(to, subject, body)


def send_verification(db: DBSession, user: User, org: Org) -> str:
    raw = issue_token(db, TokenPurpose.VERIFY_EMAIL, org_id=org.id, user_id=user.id, email=user.email)
    send_account_email(user.email, "Confirm your email for Baaki",
                       f"Hello{' ' + user.name if user.name else ''},\n\n"
                       f"Confirm this address to finish setting up {org.name} on Baaki:\n\n"
                       f"{base_url()}/verify?token={raw}\n\n"
                       f"The link works for 3 days. If you didn't create this account, ignore this email.")
    return raw


def send_password_reset(db: DBSession, user: User) -> str:
    raw = issue_token(db, TokenPurpose.PASSWORD_RESET, org_id=user.org_id, user_id=user.id, email=user.email)
    send_account_email(user.email, "Reset your Baaki password",
                       f"Someone asked to reset the password for this address.\n\n"
                       f"{base_url()}/reset?token={raw}\n\n"
                       f"The link works for 1 hour and can be used once. "
                       f"If this wasn't you, no action is needed — your password hasn't changed.")
    return raw


def send_invite(db: DBSession, org: Org, inviter: User, email: str, role: Role) -> str:
    raw = issue_token(db, TokenPurpose.INVITE, org_id=org.id, email=email, role=role, created_by=inviter.id)
    send_account_email(email, f"{inviter.name or inviter.email} invited you to {org.name} on Baaki",
                       f"{inviter.name or inviter.email} has invited you to join {org.name} on Baaki "
                       f"as a {role.value}.\n\n{base_url()}/invite?token={raw}\n\n"
                       f"The invitation expires in 7 days.")
    return raw
