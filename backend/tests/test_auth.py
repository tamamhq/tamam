"""Auth seam security tests — prove tokens are actually verified, not trusted.

Uses a self-signed RS256 keypair as a stand-in for Clerk: we mint tokens, point the
verifier at our public key, and assert valid tokens pass and tampered / expired /
wrong-issuer / wrong-party tokens are rejected. No network, no real Clerk.
"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from tamam.auth import Principal, require_auth, verify_token
from tamam.config import Settings

ISSUER = "https://test.clerk.example"


@pytest.fixture
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


def _settings(**over) -> Settings:
    base = dict(clerk_issuer=ISSUER, clerk_authorized_party="https://app.example")
    base.update(over)
    return Settings(**base)


def _mint(private_key, **claims) -> str:
    payload = {"iss": ISSUER, "sub": "user_123", "iat": int(time.time()),
               "exp": int(time.time()) + 3600, "azp": "https://app.example"}
    payload.update(claims)
    return jwt.encode(payload, private_key, algorithm="RS256")


def _patch_key(monkeypatch, public_key):
    class _SK:
        key = public_key

    class _Client:
        def get_signing_key_from_jwt(self, token):
            return _SK()

    monkeypatch.setattr("tamam.auth._JWKSCache.client", classmethod(lambda cls, url: _Client()))


def test_valid_token_yields_principal(keypair, monkeypatch):
    priv, pub = keypair
    _patch_key(monkeypatch, pub)
    p = verify_token(_mint(priv, org_id="org_abc", org_role="admin"), _settings())
    assert isinstance(p, Principal)
    assert p.user_id == "user_123" and p.org_id == "org_abc" and p.org_role == "admin"


def test_no_org_falls_back_to_personal_workspace(keypair, monkeypatch):
    priv, pub = keypair
    _patch_key(monkeypatch, pub)
    p = verify_token(_mint(priv), _settings())
    assert p.org_id == "user:user_123"  # isolated personal tenant, never blank


def test_tampered_signature_rejected(keypair, monkeypatch):
    priv, pub = keypair
    _patch_key(monkeypatch, pub)
    token = _mint(priv) + "x"  # corrupt the signature
    with pytest.raises(HTTPException) as e:
        verify_token(token, _settings())
    assert e.value.status_code == 401


def test_expired_token_rejected(keypair, monkeypatch):
    priv, pub = keypair
    _patch_key(monkeypatch, pub)
    token = _mint(priv, exp=int(time.time()) - 10)
    with pytest.raises(HTTPException):
        verify_token(token, _settings())


def test_wrong_issuer_rejected(keypair, monkeypatch):
    priv, pub = keypair
    _patch_key(monkeypatch, pub)
    token = _mint(priv, iss="https://evil.example")
    with pytest.raises(HTTPException):
        verify_token(token, _settings())


def test_wrong_authorized_party_rejected(keypair, monkeypatch):
    priv, pub = keypair
    _patch_key(monkeypatch, pub)
    token = _mint(priv, azp="https://evil.example")
    with pytest.raises(HTTPException):
        verify_token(token, _settings())


async def test_require_auth_denies_when_unconfigured(monkeypatch):
    # No issuer, no dev mode → secure default is DENY, never allow. (Explicit values
    # override any .env so the test is hermetic.)
    monkeypatch.setattr(
        "tamam.auth.get_settings",
        lambda: Settings(clerk_issuer=None, auth_dev_mode=False),
    )
    with pytest.raises(HTTPException) as e:
        await require_auth(authorization=None)
    assert e.value.status_code == 401


async def test_require_auth_dev_mode_injects_principal(monkeypatch):
    # clerk_issuer=None makes the test's premise (dev mode, no issuer) explicit and
    # hermetic — otherwise a developer's local .env TAMAM_CLERK_ISSUER leaks in and the
    # production branch 401s (CI has no .env, so this only bites locally).
    monkeypatch.setattr("tamam.auth.get_settings",
                        lambda: Settings(auth_dev_mode=True, auth_dev_org_id="org_dev",
                                         clerk_issuer=None))
    p = await require_auth(authorization=None)
    assert p.org_id == "org_dev" and p.user_id == "user_dev"


def test_dev_mode_answers_only_this_machine():
    from tamam.auth import is_local_request
    assert is_local_request("localhost:8000", None)
    assert is_local_request("127.0.0.1:8000", "http://localhost:5173")   # the dashboard's dev proxy
    assert is_local_request("[::1]:8000", "http://[::1]:5173")
    assert not is_local_request("evil.example:8000", None)               # DNS rebinding
    assert not is_local_request("localhost:8000", "https://evil.example")  # another site's page
    assert not is_local_request("localhost:8000", "null")
    assert not is_local_request(None, None)
