#!/usr/bin/env python3
"""Remediate and verify the container's eMudhra incident trust paths."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
from pathlib import Path

EMSIGN_ECC_G3_SHA256 = "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b"
AAA_CERTIFICATE_SERVICES_SHA256 = "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4"
UNSAFE_ROOTS = {
    EMSIGN_ECC_G3_SHA256: "emSign ECC Root CA - G3",
    AAA_CERTIFICATE_SERVICES_SHA256: "AAA Certificate Services",
}

# certifi 2026.10.19 restored these unaffected emSign roots after the initial
# incident release removed every emSign root.
UNAFFECTED_EMSIGN_ROOTS = {
    "40f6af0346a99aa1cd1d555a4e9cce62c7f9634603ee406615833dc8c8d00367",
    "125609aa301da0a249b97a8239cb6a34216f44dcac9f3954b14292f2e8c8608f",
    "bc4d809b15189d78db3e1d8cf4f9726a795da1643ca5f1358e1ddb0edc0d7eb3",
}

_PEM_CERTIFICATE = re.compile(
    rb"-----BEGIN CERTIFICATE-----\s+.*?-----END CERTIFICATE-----", re.DOTALL
)


def _version_tuple(version: str) -> tuple[int, ...]:
    match = re.fullmatch(r"\d+(?:\.\d+)+", version)
    if match is None:
        raise ValueError(f"unexpected version: {version!r}")
    return tuple(int(part) for part in version.split("."))


def _bundle_fingerprints(path: Path) -> set[str]:
    try:
        contents = path.read_bytes()
    except OSError as error:
        raise RuntimeError(f"cannot read CA bundle {path}: {error}") from error

    fingerprints: set[str] = set()
    for match in _PEM_CERTIFICATE.finditer(contents):
        pem = match.group().decode("ascii")
        der = ssl.PEM_cert_to_DER_cert(pem)
        fingerprints.add(hashlib.sha256(der).hexdigest())
    if not fingerprints:
        raise RuntimeError(f"CA bundle {path} contains no parseable certificates")
    return fingerprints


def _context_fingerprints(context: ssl.SSLContext) -> set[str]:
    fingerprints = {
        hashlib.sha256(certificate).hexdigest()
        for certificate in context.get_ca_certs(binary_form=True)
    }
    if not fingerprints:
        raise RuntimeError("SSL context contains no CA certificates")
    return fingerprints


def _capath_fingerprints(path: Path) -> set[str]:
    if not path.is_dir():
        raise RuntimeError(f"system CA directory does not exist: {path}")

    fingerprints: set[str] = set()
    for entry in path.iterdir():
        if entry.is_file():
            fingerprints.update(_bundle_fingerprints(entry))
    if not fingerprints:
        raise RuntimeError(f"system CA directory {path} contains no parseable certificates")
    return fingerprints


def _unsafe_names(fingerprints: set[str]) -> list[str]:
    return sorted(UNSAFE_ROOTS[fingerprint] for fingerprint in fingerprints & UNSAFE_ROOTS.keys())


def _assert_safe(label: str, fingerprints: set[str]) -> None:
    unsafe = _unsafe_names(fingerprints)
    if unsafe:
        raise RuntimeError(f"{label} still trusts: {', '.join(unsafe)}")


def _check_system_trust() -> int:
    paths = ssl.get_default_verify_paths()
    if paths.cafile is None or paths.capath is None:
        raise RuntimeError(f"system CA paths are incomplete: {paths}")

    default_roots = _context_fingerprints(ssl.create_default_context())
    cafile_roots = _bundle_fingerprints(Path(paths.cafile))
    capath_roots = _capath_fingerprints(Path(paths.capath))

    _assert_safe("the system default SSL context", default_roots)
    _assert_safe(f"the system CA file {paths.cafile}", cafile_roots)
    _assert_safe(f"the system CA directory {paths.capath}", capath_roots)
    return len(default_roots)


def _configured_ca_path(line: str) -> str | None:
    value = line.strip()
    if not value or value.startswith("#") or value.startswith("!"):
        return None
    return value


def remediate_system_trust() -> None:
    config_path = Path("/etc/ca-certificates.conf")
    source_dir = Path("/usr/share/ca-certificates")
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    disabled: dict[str, str] = {}

    for index, line in enumerate(lines):
        relative_path = _configured_ca_path(line)
        if relative_path is None:
            continue
        fingerprints = _bundle_fingerprints(source_dir / relative_path)
        unsafe = fingerprints & UNSAFE_ROOTS.keys()
        if not unsafe:
            continue

        newline = "\n" if line.endswith("\n") else ""
        lines[index] = f"!{relative_path}{newline}"
        for fingerprint in unsafe:
            disabled[fingerprint] = relative_path

    if disabled:
        config_path.write_text("".join(lines), encoding="utf-8")

    subprocess.run(["update-ca-certificates", "--fresh"], check=True)
    root_count = _check_system_trust()
    names = ", ".join(UNSAFE_ROOTS[fingerprint] for fingerprint in sorted(disabled))
    print(f"System CA trust verified ({root_count} roots); disabled: {names or 'already absent'}")


def _pip_details() -> tuple[str, Path]:
    command = (
        "import json, pip, pip._vendor.certifi as certifi; "
        "print(json.dumps({'version': pip.__version__, 'bundle': certifi.where()}))"
    )
    result = subprocess.run(
        ["/usr/local/bin/python", "-c", command],
        check=True,
        capture_output=True,
        text=True,
    )
    details = json.loads(result.stdout)
    return str(details["version"]), Path(details["bundle"])


def _uv_version() -> str:
    result = subprocess.run(["uv", "--version"], check=True, capture_output=True, text=True)
    parts = result.stdout.strip().split()
    if len(parts) < 2 or parts[0] != "uv":
        raise RuntimeError(f"unexpected uv version output: {result.stdout!r}")
    return parts[1]


def check_image_trust() -> None:
    import certifi
    import httpx

    if os.environ.get("UV_SYSTEM_CERTS", "").lower() not in {"1", "true"}:
        raise RuntimeError("uv is not configured to use the remediated system CA store")

    if _version_tuple(certifi.__version__) < _version_tuple("2026.10.19"):
        raise RuntimeError(f"certifi {certifi.__version__} is older than 2026.10.19")

    certifi_roots = _bundle_fingerprints(Path(certifi.where()))
    httpx_roots = _context_fingerprints(httpx.create_ssl_context())
    _assert_safe("the certifi bundle", certifi_roots)
    _assert_safe("HTTPX's default SSL context", httpx_roots)
    if httpx_roots != certifi_roots:
        raise RuntimeError("HTTPX's default SSL context does not match the certifi bundle")

    missing_unaffected = UNAFFECTED_EMSIGN_ROOTS - certifi_roots
    if missing_unaffected:
        raise RuntimeError("the certifi bundle is missing unaffected emSign roots")

    pip_version, pip_bundle = _pip_details()
    if _version_tuple(pip_version) < _version_tuple("26.2.2"):
        raise RuntimeError(f"pip {pip_version} is older than 26.2.2")
    _assert_safe("pip's vendored certifi bundle", _bundle_fingerprints(pip_bundle))

    uv_version = _uv_version()
    if _version_tuple(uv_version) < _version_tuple("0.13.4"):
        raise RuntimeError(f"uv {uv_version} is older than 0.13.4")

    system_root_count = _check_system_trust()
    print(
        "CA trust verified: "
        f"HTTPX uses {len(httpx_roots)} certifi {certifi.__version__} roots; "
        f"pip {pip_version}; uv {uv_version}; {system_root_count} system roots"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--remediate-system",
        action="store_true",
        help="disable the incident trust anchors in Debian's system store",
    )
    args = parser.parse_args()

    if args.remediate_system:
        if os.geteuid() != 0:
            parser.error("--remediate-system must run as root")
        remediate_system_trust()
    else:
        check_image_trust()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"CA trust check failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
