import base64
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.hashes import SHA256
from fastapi.testclient import TestClient

from app import main
from app.services import anonymous_sessions
from app.services.anonymous_sessions import hash_identity_subject
from app.services.cail_identity import (
    CAIL_IDENTITY_AUDIENCE,
    CAIL_IDENTITY_HEADER,
    CAIL_IDENTITY_ISSUER,
    CailIdentityConfigError,
    load_cail_identity_verifier,
)

_KID = "pdf-accessibility-test-key"
_SUBJECT = "cail-0123456789abcdef0123456789abcdef"
_OPERATIONAL_SUBJECT = "cail-v1-0123456789abcdef0123456789abcdef"
_NOW = 1_800_000_000


def _encode(value: object) -> str:
    return (
        base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode("utf-8"))
        .rstrip(b"=")
        .decode("ascii")
    )


def _encode_uint(value: int) -> str:
    return (
        base64.urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, "big"))
        .rstrip(b"=")
        .decode("ascii")
    )


@pytest.fixture(scope="module")
def signing_material():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": _KID,
        "alg": "RS256",
        "use": "sig",
        "n": _encode_uint(numbers.n),
        "e": _encode_uint(numbers.e),
    }
    return private_key, jwk


def _jwks(jwk: dict[str, object]) -> str:
    return json.dumps({"keys": [jwk]}, separators=(",", ":"))


def _mint(
    private_key,
    *,
    claims: dict[str, object] | None = None,
    header: dict[str, object] | None = None,
) -> str:
    protected = {
        "alg": "RS256",
        "kid": _KID,
        "typ": "JWT",
        **(header or {}),
    }
    payload = {
        "iss": CAIL_IDENTITY_ISSUER,
        "sub": _SUBJECT,
        "log_sub": _OPERATIONAL_SUBJECT,
        "aud": CAIL_IDENTITY_AUDIENCE,
        "iat": _NOW,
        "exp": _NOW + 300,
        "jti": "123e4567-e89b-42d3-a456-426614174000",
        "entitlements": ["cail-pdf-accessibility"],
        **(claims or {}),
    }
    material = f"{_encode(protected)}.{_encode(payload)}"
    signature = private_key.sign(
        material.encode("ascii"),
        padding.PKCS1v15(),
        SHA256(),
    )
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{material}.{encoded_signature}"


def _verifier(jwk: dict[str, object]):
    return load_cail_identity_verifier(
        jwks_json=_jwks(jwk),
        issuer=CAIL_IDENTITY_ISSUER,
        audience=CAIL_IDENTITY_AUDIENCE,
        clock_tolerance_seconds=60,
    )


def test_accepts_valid_audience_bound_assertion(signing_material):
    private_key, jwk = signing_material

    identity = _verifier(jwk).verify(_mint(private_key), now=_NOW)

    assert identity is not None
    assert identity.subject == _SUBJECT
    assert identity.operational_subject == _OPERATIONAL_SUBJECT
    assert identity.entitlements == ("cail-pdf-accessibility",)


@pytest.mark.parametrize(
    ("claims", "header"),
    [
        ({}, {"alg": "HS256"}),
        ({}, {"kid": "unreviewed-key"}),
        ({"iss": "https://issuer.invalid"}, {}),
        ({"aud": "cail:other-app"}, {}),
        ({"aud": [CAIL_IDENTITY_AUDIENCE]}, {}),
        ({"exp": _NOW - 1_000}, {}),
        ({"nbf": _NOW + 1_000}, {}),
        ({"sub": None}, {}),
        ({"sub": "person@example.invalid"}, {}),
        ({"log_sub": "cail-v1-invalid"}, {}),
        ({}, {"crit": ["exp"]}),
        ({}, {"b64": False}),
    ],
)
def test_rejects_invalid_assertions(signing_material, claims, header):
    private_key, jwk = signing_material

    assert (
        _verifier(jwk).verify(
            _mint(private_key, claims=claims, header=header),
            now=_NOW,
        )
        is None
    )


def test_rejects_wrong_signature(signing_material):
    _, jwk = signing_material
    unrelated_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    assert _verifier(jwk).verify(_mint(unrelated_key), now=_NOW) is None


@pytest.mark.parametrize(
    "token",
    [
        "",
        "a.b",
        "a.b.c.d",
        "not-a-jwt",
        "eyJhbGciOiJSUzI1NiJ9.e30=.AA",
        "eyJhbGciOiJSUzI1NiJ9.e30.AA==",
    ],
)
def test_rejects_malformed_compact_tokens(signing_material, token):
    _, jwk = signing_material

    assert _verifier(jwk).verify(token, now=_NOW) is None


def test_filters_non_string_entitlements(signing_material):
    private_key, jwk = signing_material

    identity = _verifier(jwk).verify(
        _mint(
            private_key,
            claims={"entitlements": ["cail-pdf-accessibility", 42, None]},
        ),
        now=_NOW,
    )

    assert identity is not None
    assert identity.entitlements == ("cail-pdf-accessibility",)


@pytest.mark.parametrize(
    ("jwks_json", "issuer", "audience", "tolerance", "reason"),
    [
        ("", CAIL_IDENTITY_ISSUER, CAIL_IDENTITY_AUDIENCE, 60, "jwks_missing"),
        ("{}", CAIL_IDENTITY_ISSUER, CAIL_IDENTITY_AUDIENCE, 60, "jwks_malformed"),
        (
            '{"keys":[]}',
            CAIL_IDENTITY_ISSUER,
            CAIL_IDENTITY_AUDIENCE,
            60,
            "jwks_malformed",
        ),
        (
            '{"keys":[{"kty":"oct","kid":"bad","k":"AA"}]}',
            CAIL_IDENTITY_ISSUER,
            CAIL_IDENTITY_AUDIENCE,
            60,
            "jwks_malformed",
        ),
        (
            "{}",
            "https://issuer.invalid",
            CAIL_IDENTITY_AUDIENCE,
            60,
            "issuer_unsupported",
        ),
        (
            "{}",
            CAIL_IDENTITY_ISSUER,
            "cail:other-app",
            60,
            "audience_malformed",
        ),
        (
            "{}",
            CAIL_IDENTITY_ISSUER,
            CAIL_IDENTITY_AUDIENCE,
            301,
            "timing_invalid",
        ),
    ],
)
def test_configuration_failures_are_distinct(
    jwks_json,
    issuer,
    audience,
    tolerance,
    reason,
):
    with pytest.raises(CailIdentityConfigError, match=reason):
        load_cail_identity_verifier(
            jwks_json=jwks_json,
            issuer=issuer,
            audience=audience,
            clock_tolerance_seconds=tolerance,
        )


def test_rejects_private_duplicate_and_weak_jwks(signing_material):
    _, jwk = signing_material
    private_jwk = {**jwk, "d": "AA"}
    duplicate_jwks = json.dumps({"keys": [jwk, jwk]})
    weak_key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    weak_numbers = weak_key.public_key().public_numbers()
    weak_jwk = {
        **jwk,
        "n": _encode_uint(weak_numbers.n),
        "e": _encode_uint(weak_numbers.e),
    }

    for value in (_jwks(private_jwk), duplicate_jwks, _jwks(weak_jwk)):
        with pytest.raises(CailIdentityConfigError, match="jwks_malformed"):
            load_cail_identity_verifier(
                jwks_json=value,
                issuer=CAIL_IDENTITY_ISSUER,
                audience=CAIL_IDENTITY_AUDIENCE,
            )


def test_identity_owner_hash_is_stable_and_domain_separated():
    first = hash_identity_subject(_SUBJECT)
    second = hash_identity_subject(_SUBJECT)

    assert first == second
    assert first != _SUBJECT.removeprefix("cail-")
    assert len(first) == 64
    with pytest.raises(ValueError, match="not canonical"):
        hash_identity_subject("person@example.invalid")


def _settings(*, required: bool, jwks: str) -> SimpleNamespace:
    return SimpleNamespace(
        cail_identity_required=required,
        cail_identity_jwks=jwks,
        cail_identity_issuer=CAIL_IDENTITY_ISSUER,
        cail_identity_clock_tolerance_seconds=60,
        cors_allow_origins="http://testserver",
        csrf_protection_enabled=False,
        anonymous_session_cookie_name="anon_session",
        anonymous_session_csrf_cookie_name="anon_session_csrf",
        anonymous_session_cookie_max_age_hours=24,
        anonymous_session_cookie_secure=False,
    )


async def _noop_async(*args, **kwargs):
    del args, kwargs
    return 0


class _JobManager:
    async def shutdown(self):
        return None


def _app_client(monkeypatch, tmp_path, settings):
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<!doctype html><title>PDF</title>")
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(anonymous_sessions, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "ensure_dirs", lambda: None)
    monkeypatch.setattr(main, "init_db", _noop_async)
    monkeypatch.setattr(main, "_cleanup_expired_jobs_once", _noop_async)
    monkeypatch.setattr(main, "_fail_abandoned_jobs_once", _noop_async)
    monkeypatch.setattr(main, "get_job_manager", lambda: _JobManager())
    return TestClient(main.create_app(frontend_dist_dir=dist_dir))


def test_required_boundary_accepts_valid_and_rejects_missing_or_invalid(
    signing_material,
    monkeypatch,
    tmp_path,
):
    private_key, jwk = signing_material
    with _app_client(monkeypatch, tmp_path, _settings(required=True, jwks=_jwks(jwk))) as client:
        missing = client.get("/")
        invalid = client.get("/", headers={CAIL_IDENTITY_HEADER: "not-a-jwt"})
        valid = client.get(
            "/",
            headers={CAIL_IDENTITY_HEADER: _mint(private_key)},
        )

    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "invalid_credential"
    assert missing.headers["x-should-retry"] == "false"
    assert invalid.status_code == 401
    assert valid.status_code == 200


def test_bad_configuration_fails_closed_but_health_stays_public(monkeypatch, tmp_path):
    with _app_client(monkeypatch, tmp_path, _settings(required=True, jwks="{}")) as client:
        protected = client.get("/")
        health = client.get("/health")

    assert protected.status_code == 503
    assert protected.json()["error"]["code"] == "identity_unavailable"
    assert protected.headers["x-should-retry"] == "true"
    assert health.status_code == 200


def test_unconfigured_optional_boundary_preserves_current_local_behavior(monkeypatch, tmp_path):
    with _app_client(monkeypatch, tmp_path, _settings(required=False, jwks="")) as client:
        response = client.get("/")

    assert response.status_code == 200
