"""Sealed transport: AEAD envelopes carried over plain HTTP.

Why this exists
---------------

`README` §6 assumes TLS is terminated by something in front of this process — a
reverse proxy, an SSH tunnel, Tailscale. When it is not, every request puts the
API key on the wire in cleartext, and that key is the *entire* authorization
boundary: whoever reads it can drive agents that run with `bypassPermissions`.

This module removes the key from the wire instead of wrapping the wire. Both
ends derive one AES-256-GCM key from the shared API key and exchange sealed
envelopes. The key itself is never transmitted; a caller proves it holds the key
by producing a tag the server can verify, which makes *authentication* and
*confidentiality* the same operation.

What it is not
--------------

This is a pre-shared-key scheme, and it is weaker than TLS in ways that matter:

- **No forward secrecy.** One key, derived once. Someone who records traffic
  today and learns the API key later can decrypt all of it retroactively. TLS
  negotiates ephemeral keys precisely to avoid this.
- **No certificate authority.** The peer is authenticated as "holds the API key"
  and nothing more. There is no name binding, so this cannot tell you that you
  reached the right host — only that whoever answered knows the secret.
- **Metadata is public.** Method, path, query string, status code, body length
  and timing all travel in the clear. Only the bodies are hidden.

It is a real mitigation for the specific problem of a plaintext credential
crossing an untrusted network, and it is not a general substitute for TLS. Put
TLS in front when that is an option.

The wire format
---------------

One envelope is a compact ASCII string, safe in a header or a body::

    v1.<base64url nonce>.<base64url ciphertext||tag>

Every request carries an `X-CC-Auth` envelope whose plaintext is the claim set
below; a request that also has a body carries that body as a second envelope.
The claims bind the auth envelope to one method, one path and one body, so a
captured envelope cannot be replayed against a different route or re-pointed at
substituted ciphertext::

    {"m": "POST", "p": "/sessions", "ts": 1765..., "bh": "<sha256 of the
     sealed body, hex>", "ct": "application/json"}

Freshness is `ts` within `SKEW_S`, plus a nonce cache so a capture cannot be
replayed inside that window.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from collections import OrderedDict
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

#: Bumped only for an incompatible envelope change; both ends check it.
VERSION = "v1"
#: 96 bits, the size AES-GCM is defined for. Random per envelope.
NONCE_BYTES = 12
#: How far a claim's `ts` may sit from the server's clock, in seconds.
SKEW_S = 300
#: Nonces remembered for replay rejection. Bounded so uptime cannot grow it.
NONCE_CACHE = 8192

#: Names of the two request headers that mark and authenticate a sealed call.
SEALED_HEADER = "x-cc-sealed"
AUTH_HEADER = "x-cc-auth"

_HKDF_SALT = b"cc-automation/sealed/v1"
_HKDF_INFO = b"aes-256-gcm"

# Domain separation: an envelope sealed for one position in the protocol must
# not verify in another, so a response cannot be fed back as a request.
AAD_AUTH = b"cc-automation/sealed/v1/auth"
AAD_REQUEST = b"cc-automation/sealed/v1/request"
AAD_RESPONSE = b"cc-automation/sealed/v1/response"
AAD_SSE = b"cc-automation/sealed/v1/sse"


class SealError(Exception):
    """An envelope did not verify, or its claims were unacceptable.

    Deliberately carries no detail about *which* check failed when it reaches a
    caller: "bad tag" and "stale timestamp" are one answer on the wire, so the
    error cannot be used to probe the server.
    """


def derive_key(api_key: str) -> bytes:
    """The API key -> a 256-bit AES key, via HKDF-SHA256.

    The API key is a human-handled string of unknown length and entropy, which
    is not a key. HKDF gives a uniform 32 bytes and keeps the transport key
    distinct from the credential itself: a server that stopped sealing would
    still compare the original string, and this derivation cannot be run
    backwards to recover it.
    """
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=_HKDF_INFO,
    ).derive(api_key.encode("utf-8"))


def b64u(raw: bytes) -> str:
    """Unpadded base64url — no `+`, `/` or `=` to escape in a header."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def unb64u(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + pad)
    except (ValueError, TypeError) as exc:
        raise SealError("malformed envelope") from exc


def seal(key: bytes, plaintext: bytes, aad: bytes) -> str:
    """`plaintext` -> `v1.<nonce>.<ciphertext>`, bound to `aad`."""
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    return f"{VERSION}.{b64u(nonce)}.{b64u(ciphertext)}"


def unseal(key: bytes, envelope: str, aad: bytes) -> bytes:
    """The inverse of `seal`, raising `SealError` on anything unverified."""
    return _open(key, envelope, aad)[1]


def _split(envelope: str) -> tuple[bytes, bytes]:
    parts = envelope.strip().split(".")
    if len(parts) != 3 or parts[0] != VERSION:
        raise SealError("malformed envelope")
    nonce, ciphertext = unb64u(parts[1]), unb64u(parts[2])
    if len(nonce) != NONCE_BYTES:
        raise SealError("malformed envelope")
    return nonce, ciphertext


def _open(key: bytes, envelope: str, aad: bytes) -> tuple[bytes, bytes]:
    """-> `(nonce, plaintext)`. The nonce is what the replay guard keys on."""
    nonce, ciphertext = _split(envelope)
    try:
        return nonce, AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise SealError("envelope did not verify") from exc


def body_hash(sealed_body: bytes) -> str:
    """What `bh` holds: the digest of the body *as sent*, ciphertext and all.

    Hashing the sealed bytes rather than the plaintext means the binding can be
    checked before anything is decrypted, and a swapped body is refused by the
    claims rather than by a second tag failure.
    """
    return hashlib.sha256(sealed_body).hexdigest()


@dataclass(frozen=True)
class Claims:
    """What the caller asserts it is doing, authenticated by the auth envelope."""

    method: str
    path: str
    ts: int
    body_sha256: str | None = None
    content_type: str | None = None

    def as_dict(self) -> dict:
        payload = {"m": self.method, "p": self.path, "ts": self.ts}
        if self.body_sha256 is not None:
            payload["bh"] = self.body_sha256
        if self.content_type is not None:
            payload["ct"] = self.content_type
        return payload


def seal_claims(key: bytes, claims: Claims) -> str:
    return seal(key, json.dumps(claims.as_dict()).encode("utf-8"), AAD_AUTH)


def open_claims(key: bytes, envelope: str) -> tuple[bytes, Claims]:
    """-> `(nonce, claims)`, verified as ciphertext but not yet as a request."""
    nonce, plaintext = _open(key, envelope, AAD_AUTH)
    try:
        payload = json.loads(plaintext)
    except ValueError as exc:
        raise SealError("malformed claims") from exc
    if not isinstance(payload, dict):
        raise SealError("malformed claims")
    method, path, ts = payload.get("m"), payload.get("p"), payload.get("ts")
    if not isinstance(method, str) or not isinstance(path, str):
        raise SealError("malformed claims")
    if not isinstance(ts, int) or isinstance(ts, bool):
        raise SealError("malformed claims")
    return nonce, Claims(
        method=method,
        path=path,
        ts=ts,
        body_sha256=payload.get("bh"),
        content_type=payload.get("ct"),
    )


class NonceGuard:
    """Nonces seen inside the skew window, so a capture cannot be replayed.

    The timestamp check alone leaves a `SKEW_S`-wide hole: an envelope copied
    off the wire is valid until it goes stale. Remembering nonces closes it, and
    only for that window — anything older is refused by `ts` anyway, so the
    cache never has to be larger than the traffic of one window.
    """

    def __init__(self, skew_s: int = SKEW_S, capacity: int = NONCE_CACHE) -> None:
        self.skew_s = skew_s
        self.capacity = capacity
        self._seen: OrderedDict[bytes, float] = OrderedDict()

    def check(self, nonce: bytes, ts: int, now: float | None = None) -> None:
        """Accept `nonce` once, within the window. Raises `SealError` twice."""
        now = time.time() if now is None else now
        if abs(now - ts) > self.skew_s:
            raise SealError("stale or future-dated request")
        self._evict(now)
        if nonce in self._seen:
            raise SealError("replayed request")
        self._seen[nonce] = now
        self._seen.move_to_end(nonce)
        while len(self._seen) > self.capacity:
            self._seen.popitem(last=False)

    def _evict(self, now: float) -> None:
        while self._seen:
            oldest, seen_at = next(iter(self._seen.items()))
            if now - seen_at <= self.skew_s:
                return
            del self._seen[oldest]


class SealedSession:
    """One derived key plus its replay guard: everything a peer needs.

    The server holds one of these for the lifetime of the process; a client
    builds one per session. Both sides run the same code, which is the point —
    a mismatch in framing between two implementations is exactly the kind of bug
    that fails open.
    """

    def __init__(self, api_key: str, skew_s: int = SKEW_S) -> None:
        self.key = derive_key(api_key)
        self.guard = NonceGuard(skew_s=skew_s)

    # -- client side --------------------------------------------------------

    def seal_request(
        self, method: str, path: str, body: bytes | None, content_type: str | None
    ) -> tuple[str, bytes | None]:
        """-> `(auth envelope, sealed body or None)` for one outbound request."""
        sealed_body = (
            seal(self.key, body, AAD_REQUEST).encode("ascii") if body else None
        )
        claims = Claims(
            method=method.upper(),
            path=path,
            ts=int(time.time()),
            body_sha256=body_hash(sealed_body) if sealed_body else None,
            content_type=content_type if sealed_body else None,
        )
        return seal_claims(self.key, claims), sealed_body

    def open_response(self, sealed: bytes | str) -> bytes:
        text = sealed.decode("ascii") if isinstance(sealed, bytes) else sealed
        return unseal(self.key, text, AAD_RESPONSE)

    # -- server side --------------------------------------------------------

    def open_request(
        self, method: str, path: str, auth: str, body: bytes
    ) -> tuple[bytes, str | None]:
        """Verify one inbound request. -> `(plaintext body, content type)`.

        Every rejection is a `SealError`. The order matters: cryptography first,
        so the claims are never read until they are authenticated, then
        freshness, then the bindings.
        """
        nonce, claims = open_claims(self.key, auth)
        self.guard.check(nonce, claims.ts)
        if claims.method != method.upper() or claims.path != path:
            raise SealError("claims do not match the request")
        if not body:
            if claims.body_sha256 is not None:
                raise SealError("claims promise a body that is not there")
            return b"", None
        if claims.body_sha256 != body_hash(body):
            raise SealError("body does not match its claim")
        return unseal(self.key, body.decode("ascii", "replace"), AAD_REQUEST), (
            claims.content_type
        )

    def seal_response(self, body: bytes) -> bytes:
        return seal(self.key, body, AAD_RESPONSE).encode("ascii")

    def seal_event(self, chunk: str) -> str:
        return seal(self.key, chunk.encode("utf-8"), AAD_SSE)

    def open_event(self, envelope: str) -> str:
        return unseal(self.key, envelope, AAD_SSE).decode("utf-8")
