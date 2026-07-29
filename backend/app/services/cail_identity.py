import base64
import binascii
import json
import math
import re
import time
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.hashes import SHA256

CAIL_IDENTITY_HEADER = "X-CAIL-Identity-JWT"
CAIL_IDENTITY_ISSUER = "https://tools.ailab.gc.cuny.edu/cail-sso"
CAIL_IDENTITY_AUDIENCE = "cail:pdf-accessibility"

_BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_SUBJECT_PATTERN = re.compile(r"^cail-[0-9a-f]{32}$")
_OPERATIONAL_SUBJECT_PATTERN = re.compile(r"^cail-v1-[0-9a-f]{32}$")
_PRIVATE_JWK_FIELDS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth", "k"})
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_MAX_CLOCK_TOLERANCE_SECONDS = 300
_MAX_TOKEN_LENGTH = 16_384


class CailIdentityConfigError(ValueError):
    """Invalid verifier configuration; callers map this to service unavailable."""


@dataclass(frozen=True)
class CailIdentity:
    subject: str
    operational_subject: str | None
    entitlements: tuple[str, ...]


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")


def _json_object(value: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(value, parse_constant=_reject_json_constant)
    except (TypeError, UnicodeDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _decode_base64url(value: Any) -> bytes | None:
    if not isinstance(value, str) or not value or not _BASE64URL_PATTERN.fullmatch(value):
        return None
    try:
        decoded = base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))
    except (binascii.Error, ValueError):
        return None
    if not decoded or base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value:
        return None
    return decoded


def _decode_base64url_uint(value: Any) -> int | None:
    decoded = _decode_base64url(value)
    if decoded is None or decoded[0] == 0:
        return None
    return int.from_bytes(decoded, "big")


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(item, str) and item for item in value):
        return None
    return value


def _public_key_from_jwk(value: Any) -> tuple[str, rsa.RSAPublicKey] | None:
    if not isinstance(value, dict) or value.get("kty") != "RSA":
        return None
    kid = value.get("kid")
    if not isinstance(kid, str) or not kid:
        return None
    if value.get("alg", "RS256") != "RS256" or value.get("use", "sig") != "sig":
        return None
    key_ops = value.get("key_ops")
    if key_ops is not None:
        operations = _string_list(key_ops)
        if (
            operations is None
            or len(set(operations)) != len(operations)
            or "verify" not in operations
        ):
            return None
    if _PRIVATE_JWK_FIELDS.intersection(value):
        return None

    modulus = _decode_base64url_uint(value.get("n"))
    exponent = _decode_base64url_uint(value.get("e"))
    if (
        modulus is None
        or exponent is None
        or modulus.bit_length() < 2048
        or modulus % 2 == 0
        or exponent < 3
        or exponent % 2 == 0
        or exponent > _MAX_SAFE_INTEGER
    ):
        return None
    try:
        public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
    except ValueError:
        return None
    return kid, public_key


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


class CailIdentityVerifier:
    def __init__(
        self,
        *,
        keys_by_kid: dict[str, rsa.RSAPublicKey],
        issuer: str,
        audience: str,
        clock_tolerance_seconds: float,
    ):
        self._keys_by_kid = dict(keys_by_kid)
        self.issuer = issuer
        self.audience = audience
        self.clock_tolerance_seconds = clock_tolerance_seconds

    def verify(self, token: str, *, now: float | None = None) -> CailIdentity | None:
        if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_LENGTH:
            return None
        parts = token.split(".")
        if len(parts) != 3:
            return None
        decoded = [_decode_base64url(part) for part in parts]
        if any(part is None for part in decoded):
            return None

        header = _json_object(decoded[0] or b"")
        claims = _json_object(decoded[1] or b"")
        signature = decoded[2]
        if header is None or claims is None or signature is None:
            return None
        if header.get("alg") != "RS256" or "crit" in header:
            return None
        if "b64" in header and header["b64"] is not True:
            return None
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            return None
        public_key = self._keys_by_kid.get(kid)
        if public_key is None:
            return None

        subject = claims.get("sub")
        operational_subject = claims.get("log_sub")
        expiration = _finite_number(claims.get("exp"))
        not_before = _finite_number(claims["nbf"]) if "nbf" in claims else None
        if (
            not isinstance(subject, str)
            or not _SUBJECT_PATTERN.fullmatch(subject)
            or claims.get("iss") != self.issuer
            or claims.get("aud") != self.audience
            or expiration is None
            or ("nbf" in claims and not_before is None)
            or (
                operational_subject is not None
                and (
                    not isinstance(operational_subject, str)
                    or not _OPERATIONAL_SUBJECT_PATTERN.fullmatch(operational_subject)
                )
            )
        ):
            return None

        current_time = time.time() if now is None else now
        if not math.isfinite(current_time):
            return None
        if current_time >= expiration + self.clock_tolerance_seconds:
            return None
        if not_before is not None and current_time + self.clock_tolerance_seconds < not_before:
            return None

        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
        try:
            public_key.verify(signature, signing_input, padding.PKCS1v15(), SHA256())
        except (InvalidSignature, ValueError):
            return None

        raw_entitlements = claims.get("entitlements")
        entitlements = (
            tuple(item for item in raw_entitlements if isinstance(item, str))
            if isinstance(raw_entitlements, list)
            else ()
        )
        return CailIdentity(
            subject=subject,
            operational_subject=operational_subject,
            entitlements=entitlements,
        )


def load_cail_identity_verifier(
    *,
    jwks_json: str,
    issuer: str,
    audience: str = CAIL_IDENTITY_AUDIENCE,
    clock_tolerance_seconds: float = 60,
) -> CailIdentityVerifier:
    if issuer != CAIL_IDENTITY_ISSUER:
        raise CailIdentityConfigError("issuer_unsupported")
    if audience != CAIL_IDENTITY_AUDIENCE:
        raise CailIdentityConfigError("audience_malformed")
    if (
        isinstance(clock_tolerance_seconds, bool)
        or not isinstance(clock_tolerance_seconds, (int, float))
        or not math.isfinite(clock_tolerance_seconds)
        or clock_tolerance_seconds < 0
        or clock_tolerance_seconds > _MAX_CLOCK_TOLERANCE_SECONDS
    ):
        raise CailIdentityConfigError("timing_invalid")
    if not isinstance(jwks_json, str) or not jwks_json.strip():
        raise CailIdentityConfigError("jwks_missing")
    try:
        jwks = json.loads(jwks_json, parse_constant=_reject_json_constant)
    except (TypeError, ValueError) as error:
        raise CailIdentityConfigError("jwks_malformed") from error
    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list) or not jwks["keys"]:
        raise CailIdentityConfigError("jwks_malformed")

    keys_by_kid: dict[str, rsa.RSAPublicKey] = {}
    for raw_key in jwks["keys"]:
        loaded = _public_key_from_jwk(raw_key)
        if loaded is None:
            raise CailIdentityConfigError("jwks_malformed")
        kid, public_key = loaded
        if kid in keys_by_kid:
            raise CailIdentityConfigError("jwks_malformed")
        keys_by_kid[kid] = public_key
    return CailIdentityVerifier(
        keys_by_kid=keys_by_kid,
        issuer=issuer,
        audience=audience,
        clock_tolerance_seconds=float(clock_tolerance_seconds),
    )
