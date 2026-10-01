"""Test helpers for Hardening Phase 9: a local OIDC issuer (a real HTTP server on 127.0.0.1
serving discovery and JWKS), a token signer, a local Vault KV v2 server and a throwaway CA.

Everything is generated at run time; no key material is stored in the repository."""

from __future__ import annotations

import base64
import datetime as dt
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import quote

from cryptography import x509
from cryptography.hazmat.primitives import hashes, hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import NameOID


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _uint(value: int, size: int | None = None) -> str:
    length = size or (value.bit_length() + 7) // 8
    return b64url(value.to_bytes(length, "big"))


@dataclass
class SigningKey:
    kid: str
    alg: str
    private: Any

    @classmethod
    def rsa(cls, kid: str, alg: str = "RS256") -> SigningKey:
        return cls(kid, alg, rsa.generate_private_key(public_exponent=65537, key_size=2048))

    @classmethod
    def ec(cls, kid: str) -> SigningKey:
        return cls(kid, "ES256", ec.generate_private_key(ec.SECP256R1()))

    def jwk(self) -> dict[str, Any]:
        pub = self.private.public_key()
        if isinstance(pub, rsa.RSAPublicKey):
            n = pub.public_numbers()
            return {"kty": "RSA", "kid": self.kid, "alg": self.alg, "use": "sig",
                    "n": _uint(n.n), "e": _uint(n.e)}
        n = pub.public_numbers()
        return {"kty": "EC", "kid": self.kid, "alg": self.alg, "use": "sig", "crv": "P-256",
                "x": _uint(n.x, 32), "y": _uint(n.y, 32)}

    def public_pem(self) -> bytes:
        return self.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def sign(claims: dict[str, Any], key: SigningKey | None, *, alg: str | None = None,
         kid: str | None = "__key__", hmac_secret: bytes | None = None) -> str:
    """A compact JWS. ``alg`` overrides the header (for forgeries); ``hmac_secret`` signs with
    HS256 (the classic public-key-as-secret attack); ``key=None`` with ``alg="none"`` makes an
    unsigned token."""
    header: dict[str, Any] = {"alg": alg or (key.alg if key else "none"), "typ": "JWT"}
    if kid == "__key__" and key is not None:
        header["kid"] = key.kid
    elif kid not in (None, "__key__"):
        header["kid"] = kid
    signing_input = f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(claims).encode())}"
    data = signing_input.encode("ascii")
    if hmac_secret is not None:
        mac = hmac.HMAC(hmac_secret, hashes.SHA256())
        mac.update(data)
        return f"{signing_input}.{b64url(mac.finalize())}"
    if key is None:
        return f"{signing_input}."
    if isinstance(key.private, rsa.RSAPrivateKey):
        pad = (padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                           salt_length=padding.PSS.DIGEST_LENGTH)
               if key.alg.startswith("PS") else padding.PKCS1v15())
        signature = key.private.sign(data, pad, hashes.SHA256())
    else:
        r, s = decode_dss_signature(key.private.sign(data, ec.ECDSA(hashes.SHA256())))
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{signing_input}.{b64url(signature)}"


@dataclass
class Issuer:
    """A local OIDC issuer. ``keys`` is what the JWKS endpoint serves right now."""

    url: str = ""
    keys: list[SigningKey] = field(default_factory=list)
    jwks_hits: int = 0
    discovery_jwks_uri: str | None = None  # override (to test an inward-pointing jwks_uri)

    def claims(self, **extra: Any) -> dict[str, Any]:
        now = int(time.time())
        base = {"iss": self.url, "aud": "oran-adapt", "sub": "alice", "iat": now,
                "nbf": now, "exp": now + 300, "roles": ["oran-operator"]}
        base.update(extra)
        return base


@contextmanager
def local_issuer(*keys: SigningKey) -> Iterator[Issuer]:
    issuer = Issuer(keys=list(keys))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/.well-known/openid-configuration":
                body = {"issuer": issuer.url,
                        "jwks_uri": issuer.discovery_jwks_uri or f"{issuer.url}/jwks"}
            elif self.path == "/jwks":
                issuer.jwks_hits += 1
                body = {"keys": [k.jwk() for k in issuer.keys]}
            else:
                self.send_error(404)
                return
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:
            return

    with _serve(Handler) as url:
        issuer.url = url
        yield issuer


@contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@contextmanager
def local_vault(token: str, data: dict[str, str], path: str = "/v1/secret/data/oran-adapt"
                ) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.headers.get("X-Vault-Token") != token:
                self.send_error(403)
                return
            if self.path != path:
                self.send_error(404)
                return
            payload = json.dumps({"data": {"data": data, "metadata": {"version": 1}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:
            return

    with _serve(Handler) as url:
        yield url


# ---- certificates -----------------------------------------------------------------------------
@dataclass
class Ca:
    key: Any
    cert: x509.Certificate

    def pem(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.PEM)


def make_ca(name: str = "test-ca") -> Ca:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = dt.datetime.now(dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return Ca(key, cert)


def client_cert(ca: Ca, cn: str, *, dns: str | None = None, expired: bool = False
                ) -> x509.Certificate:
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    start, end = ((now - dt.timedelta(days=10), now - dt.timedelta(days=1)) if expired
                  else (now - dt.timedelta(days=1), now + dt.timedelta(days=1)))
    builder = (x509.CertificateBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
               .issuer_name(ca.cert.subject).public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(start).not_valid_after(end))
    if dns:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(dns)]),
                                        critical=False)
    return builder.sign(ca.key, hashes.SHA256())


def forwarded(cert: x509.Certificate, *, envoy: bool = False) -> str:
    pem = quote(cert.public_bytes(serialization.Encoding.PEM).decode("ascii"), safe="")
    return f'By=spiffe://proxy;Hash=abc;Cert="{pem}";Subject="x"' if envoy else pem
