"""Clerk as the identity provider, when it is configured.

Clerk owns identity: credentials, Google and Apple sign-in, MFA, session tokens. Baaki keeps its
own `Org`, `User` and `Role` rows, so tenancy, teams, guardrails and the audit trail are unchanged
and do not depend on a vendor's organisation model.

Without `CLERK_SECRET_KEY` this module reports itself disabled and the built-in password auth in
`security.py` handles everything. That keeps `baaki demo`, local development and the test suite
working with no third-party account.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session as DBSession, select

from .models import Org, Role, User, utcnow

SESSION_COOKIE = "__session"


def enabled() -> bool:
    return bool(os.environ.get("CLERK_SECRET_KEY"))


def publishable_key() -> str:
    return os.environ.get("CLERK_PUBLISHABLE_KEY", "")


def _secret() -> str:
    return os.environ["CLERK_SECRET_KEY"]


def _authorized_parties() -> list[str] | None:
    parties = os.environ.get("CLERK_AUTHORIZED_PARTIES", "")
    return [p.strip() for p in parties.split(",") if p.strip()] or None


# ---- session verification ----------------------------------------------------------------
def verify(request) -> dict[str, Any] | None:
    """Validate the Clerk session token on this request and return its claims.

    `jwt_key` (the instance's PEM public key) makes this networkless, which is what you want in
    the request path. Without it the SDK fetches JWKS from Clerk on each verification.
    """
    if not enabled():
        return None
    from clerk_backend_api import AuthenticateRequestOptions, authenticate_request

    state = authenticate_request(
        request,
        AuthenticateRequestOptions(
            secret_key=_secret(),
            jwt_key=os.environ.get("CLERK_JWT_KEY") or None,
            authorized_parties=_authorized_parties(),
        ),
    )
    if not state.is_signed_in:
        return None
    return dict(state.payload or {})


def fetch_user(clerk_user_id: str):
    from clerk_backend_api import Clerk

    with Clerk(bearer_auth=_secret()) as clerk:
        return clerk.users.get(user_id=clerk_user_id)


def _primary_email(cu) -> tuple[str, bool]:
    """Returns (address, verified). Verification status decides whether we may link accounts."""
    for addr in cu.email_addresses or []:
        if addr.id == cu.primary_email_address_id:
            status = getattr(getattr(addr, "verification", None), "status", None)
            return (addr.email_address or "").lower(), status == "verified"
    for addr in cu.email_addresses or []:
        status = getattr(getattr(addr, "verification", None), "status", None)
        return (addr.email_address or "").lower(), status == "verified"
    return "", False


def _display_name(cu) -> str:
    return " ".join(p for p in (cu.first_name, cu.last_name) if p).strip() or (cu.username or "")


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "org"


# ---- provisioning ---------------------------------------------------------------------------
class ClerkProvisionError(RuntimeError):
    pass


def provision(db: DBSession, claims: dict, invite_token: str | None = None) -> User | None:
    """Map a verified Clerk session onto a local user, creating one on first sight.

    Linking rule: an existing local account is adopted only when Clerk reports the email address
    as verified. Linking on an unverified address would let anyone who signs up at Clerk with
    someone else's email walk into that org.
    """
    clerk_user_id = claims.get("sub")
    if not clerk_user_id:
        return None

    user = db.exec(select(User).where(User.clerk_user_id == clerk_user_id)).first()
    if user:
        return None if user.disabled else user

    cu = fetch_user(clerk_user_id)
    email, verified = _primary_email(cu)
    if not email:
        raise ClerkProvisionError("Your identity provider did not supply an email address.")

    name = _display_name(cu)
    avatar = getattr(cu, "image_url", "") or ""

    # 1. An invitation takes precedence: join that org with the invited role.
    if invite_token:
        from .accounts import consume_token
        from .models import TokenPurpose

        row = consume_token(db, invite_token, TokenPurpose.INVITE)
        if row and row.email == email:
            user = User(org_id=row.org_id, email=email, name=name, role=row.role,
                        clerk_user_id=clerk_user_id, avatar_url=avatar,
                        email_verified_at=utcnow() if verified else None)
            db.add(user); db.commit(); db.refresh(user)
            return user

    # 2. An existing local account with a verified matching address is adopted, not duplicated.
    existing = db.exec(select(User).where(User.email == email)).first()
    if existing:
        if not verified:
            raise ClerkProvisionError(
                f"An account already exists for {email}. Verify that address with your identity "
                f"provider before signing in this way."
            )
        if existing.disabled:
            return None
        existing.clerk_user_id = clerk_user_id
        existing.avatar_url = avatar or existing.avatar_url
        existing.name = existing.name or name
        existing.email_verified_at = existing.email_verified_at or utcnow()
        db.add(existing); db.commit(); db.refresh(existing)
        return existing

    # 3. Otherwise a brand-new organisation. Clerk knows the person's name, not their business
    #    name, so the org is parked as un-onboarded until they supply one.
    from .billing import start_trial

    base = _slugify(name or email.split("@")[0])
    slug, n = base, 1
    while db.exec(select(Org).where(Org.slug == slug)).first():
        n += 1
        slug = f"{base}-{n}"
    placeholder = f"{name}'s business" if name else email.split("@")[0]
    org = Org(name=placeholder, slug=slug, legal_name="", reply_to_email=email,
              onboarding_complete=False)
    start_trial(org)
    db.add(org); db.commit(); db.refresh(org)

    from .models import PolicySettings

    db.add(PolicySettings(org_id=org.id))
    user = User(org_id=org.id, email=email, name=name, role=Role.OWNER,
                clerk_user_id=clerk_user_id, avatar_url=avatar,
                email_verified_at=utcnow() if verified else None)
    db.add(user); db.commit(); db.refresh(user)

    from .service import DbAudit

    DbAudit(db, org.id, actor=f"user:{user.id}").record(
        "org_created", org=org.slug, by=email, via="clerk")
    db.commit()
    return user


# ---- webhooks --------------------------------------------------------------------------------
def verify_webhook(body: bytes, headers, secret: str) -> bool:
    """Svix signature check, as Clerk sends.

    Signed content is `{svix-id}.{svix-timestamp}.{body}`, HMAC-SHA256 with the base64 secret after
    its `whsec_` prefix. The header may carry several space-separated versioned signatures during
    key rotation, so any match counts.
    """
    svix_id = headers.get("svix-id")
    timestamp = headers.get("svix-timestamp")
    signatures = headers.get("svix-signature", "")
    if not (svix_id and timestamp and signatures):
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    # Reject replays of old deliveries.
    if abs(datetime.now(timezone.utc).timestamp() - ts) > 300:
        return False

    key = base64.b64decode(secret.removeprefix("whsec_"))
    expected = base64.b64encode(
        hmac.new(key, f"{svix_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    ).decode()
    return any(hmac.compare_digest(expected, part.split(",", 1)[-1])
               for part in signatures.split(" ") if part)


def apply_webhook(db: DBSession, event: dict) -> str:
    """Keep local rows in step with Clerk. Deleting a user disables rather than erases, so their
    audit trail and past approvals stay attributable."""
    kind = event.get("type", "")
    data = event.get("data") or {}
    clerk_id = data.get("id")
    if not clerk_id:
        return "ignored"
    user = db.exec(select(User).where(User.clerk_user_id == clerk_id)).first()
    if not user:
        return "unknown user"

    from .service import DbAudit

    audit = DbAudit(db, user.org_id, actor="clerk")
    if kind == "user.deleted":
        user.disabled = True
        from .models import Session as SessionRow

        for s in db.exec(select(SessionRow).where(SessionRow.user_id == user.id, SessionRow.revoked == False)).all():  # noqa: E712
            s.revoked = True
            db.add(s)
        audit.record("user_disabled_by_provider", email=user.email)
    elif kind == "user.updated":
        emails = data.get("email_addresses") or []
        primary = next((e for e in emails if e.get("id") == data.get("primary_email_address_id")), None)
        if primary and primary.get("email_address"):
            user.email = primary["email_address"].lower()
        name = " ".join(p for p in (data.get("first_name"), data.get("last_name")) if p).strip()
        if name:
            user.name = name
        user.avatar_url = data.get("image_url") or user.avatar_url
        audit.record("user_updated_by_provider", email=user.email)
    else:
        return "ignored"
    db.add(user)
    db.commit()
    return kind
