"""Clerk JWT verification against a locally generated RSA key and JWKS. No network."""

import time
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from bido_back.auth import (
    AuthConfigError,
    AuthenticationFailed,
    ClerkJwtVerifier,
    MembershipRow,
    NotAMember,
    TenantSelectionRequired,
    check_auth_settings,
    choose_membership,
    dev_mode_enabled,
)
from bido_back.settings import Settings

ISSUER = "https://clerk.example.test"
PARTY = "https://app.example.test"


class Keys:
    def __init__(self) -> None:
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = f"kid_{uuid4().hex[:8]}"

    def jwk(self) -> dict[str, object]:
        data: dict[str, object] = dict(RSAAlgorithm.to_jwk(self.private.public_key(), as_dict=True))
        data.update({"kid": self.kid, "alg": "RS256", "use": "sig"})
        return data

    def token(self, **overrides: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": "user_abc",
            "iss": ISSUER,
            "azp": PARTY,
            "iat": now,
            "nbf": now - 5,
            "exp": now + 60,
        }
        claims.update(overrides)
        for key in [k for k, v in claims.items() if v is None]:
            del claims[key]
        return jwt.encode(claims, self.private, algorithm="RS256", headers={"kid": self.kid})


class FakeJwks:
    def __init__(self, *keys: Keys) -> None:
        self.keys = list(keys)
        self.calls = 0

    async def __call__(self) -> Mapping[str, object]:
        self.calls += 1
        return {"keys": [k.jwk() for k in self.keys]}


def verifier(jwks: FakeJwks, **kwargs: Any) -> ClerkJwtVerifier:
    return ClerkJwtVerifier(issuer=ISSUER, authorized_parties=[PARTY], fetch_jwks=jwks, **kwargs)


@pytest.fixture(scope="module")
def keys() -> Keys:
    return Keys()


async def test_valid_token_returns_the_clerk_user_id(keys: Keys) -> None:
    jwks = FakeJwks(keys)
    v = verifier(jwks)

    assert await v.verify(keys.token()) == "user_abc"
    assert await v.verify(keys.token(sub="user_other")) == "user_other"
    assert jwks.calls == 1  # cached


@pytest.mark.parametrize(
    ("claims", "message"),
    [
        ({"exp": int(time.time()) - 120}, "token expired"),
        ({"nbf": int(time.time()) + 120}, "token not yet valid"),
        ({"iss": "https://evil.example.test"}, "untrusted token issuer"),
        ({"azp": "https://evil.example.test"}, "unauthorized party"),
        ({"azp": None}, "unauthorized party"),
        ({"exp": None}, "invalid token"),
        ({"nbf": None}, "invalid token"),
        ({"sub": None}, "invalid token"),
    ],
)
async def test_invalid_claims_are_refused(keys: Keys, claims: dict[str, Any], message: str) -> None:
    with pytest.raises(AuthenticationFailed, match=message):
        await verifier(FakeJwks(keys)).verify(keys.token(**claims))


async def test_signature_from_another_key_is_refused(keys: Keys) -> None:
    impostor = Keys()
    impostor.kid = keys.kid  # same kid, different key

    with pytest.raises(AuthenticationFailed, match="invalid token"):
        await verifier(FakeJwks(keys)).verify(impostor.token())


async def test_only_rs256_is_accepted(keys: Keys) -> None:
    hs = jwt.encode(
        {"sub": "user_abc", "iss": ISSUER, "azp": PARTY, "exp": int(time.time()) + 60},
        "a-shared-secret-that-is-long-enough-for-hs256",
        algorithm="HS256",
        headers={"kid": keys.kid},
    )
    with pytest.raises(AuthenticationFailed, match="unsupported token algorithm"):
        await verifier(FakeJwks(keys)).verify(hs)


async def test_garbage_is_refused(keys: Keys) -> None:
    with pytest.raises(AuthenticationFailed, match="malformed token"):
        await verifier(FakeJwks(keys)).verify("not-a-jwt")


async def test_unknown_kid_refetches_once_per_cooldown(keys: Keys) -> None:
    rotated = Keys()
    jwks = FakeJwks(keys)
    v = verifier(jwks, refetch_cooldown_s=0.0)
    assert await v.verify(keys.token()) == "user_abc"

    jwks.keys.append(rotated)  # Clerk rotated its keys
    assert await v.verify(rotated.token()) == "user_abc"
    assert jwks.calls == 2

    cooling = verifier(FakeJwks(keys), refetch_cooldown_s=3600.0)
    await cooling.verify(keys.token())
    with pytest.raises(AuthenticationFailed, match="unknown signing key"):
        await cooling.verify(Keys().token())


def _settings(**values: object) -> Settings:
    return Settings.model_validate(
        {"database_url": "postgresql+asyncpg://app@localhost:5432/unused", **values}
    )


@pytest.mark.parametrize("environment", ["test", "dev", "prd"])
def test_dev_mode_is_refused_outside_local(environment: str) -> None:
    settings = _settings(environment=environment, auth_dev_mode=True)
    with pytest.raises(AuthConfigError, match="AUTH_DEV_MODE"):
        check_auth_settings(settings)
    assert not dev_mode_enabled(settings)


def test_dev_mode_is_allowed_locally() -> None:
    settings = _settings(environment="local", auth_dev_mode=True)
    check_auth_settings(settings)
    assert dev_mode_enabled(settings)


def test_choose_membership() -> None:
    a = MembershipRow(uuid4(), uuid4(), "member")
    b = MembershipRow(uuid4(), a.user_id, "approver")

    assert choose_membership([a], None) == a
    assert choose_membership([a], str(a.tenant_id)) == a
    assert choose_membership([a, b], str(b.tenant_id)) == b
    with pytest.raises(TenantSelectionRequired):
        choose_membership([a, b], None)
    with pytest.raises(TenantSelectionRequired):
        choose_membership([a, b], "not-a-uuid")
    with pytest.raises(NotAMember):
        choose_membership([a, b], str(uuid4()))
    with pytest.raises(NotAMember):
        choose_membership([], None)
