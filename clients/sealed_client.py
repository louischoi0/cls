"""A reference client for the sealed transport, and the CLI that demos it.

`OPERATING.md`'s curl recipes stop working the moment `CC_AUTOMATION_REQUIRE_SEALED`
is on, because curl cannot build an envelope. This is the replacement: the same
calls, sealed, over plain HTTP.

    python -m clients.sealed_client GET /agents
    python -m clients.sealed_client POST /messages -d '{"text":"hi","tags":["research"]}'
    python -m clients.sealed_client --url http://10.1.0.4:9999 GET /health

It imports `server.sealed`, so client and server frame envelopes with one
implementation rather than two that agree until they don't.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.sealed import (  # noqa: E402
    AUTH_HEADER,
    SEALED_HEADER,
    SealError,
    SealedSession,
    VERSION,
)

DEFAULT_URL = "http://127.0.0.1:8787"
SEALED_MEDIA_TYPE = "application/cc-sealed"


def load_api_key(explicit: str | None = None) -> str:
    """The same resolution order the server uses, so both ends agree."""
    if explicit:
        return explicit.strip()
    env = os.environ.get("CC_AUTOMATION_API_KEY")
    if env:
        return env.strip()
    path = Path(
        os.environ.get("CC_AUTOMATION_API_KEY_FILE", "~/.cc-automation/api_key")
    ).expanduser()
    if path.is_file():
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    raise SystemExit(f"no API key: set CC_AUTOMATION_API_KEY or write one to {path}")


def _ssl_context(cafile: str | None, insecure: bool) -> ssl.SSLContext | None:
    """How to trust an `https://` server, or None when the URL is plain HTTP.

    A self-signed cert (`OPERATING.md`, **Serving TLS directly**) is not in any
    trust store, so one of these is needed to talk to it:

    `--cafile` pins that exact certificate, which still authenticates the host.
    `--insecure` turns verification off and is a different thing entirely — it
    keeps the encryption and drops the guarantee of who is on the other end, so
    a machine-in-the-middle is undetectable. The sealed envelopes underneath
    survive it: they authenticate the peer as a key holder independently of the
    certificate. That is the only reason this option is offered at all.
    """
    if insecure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    return None


class SealedClient:
    """Sealed calls against one server. Not thread-safe; one per caller."""

    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        api_key: str | None = None,
        cafile: str | None = None,
        insecure: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = SealedSession(load_api_key(api_key))
        self.ssl_context = _ssl_context(cafile, insecure)

    def request(
        self,
        method: str,
        path: str,
        body: bytes | str | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, bytes]:
        """-> `(status, plaintext body)`. Raises `SealError` on a bad reply.

        `path` is signed exactly as written, query string included, so it has to
        be the same string the server will see.
        """
        raw = body.encode("utf-8") if isinstance(body, str) else body
        auth, sealed_body = self.session.seal_request(method, path, raw, content_type)

        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=sealed_body,
            method=method.upper(),
        )
        request.add_header(SEALED_HEADER, VERSION)
        request.add_header(AUTH_HEADER, auth)
        if sealed_body:
            request.add_header("Content-Type", SEALED_MEDIA_TYPE)

        try:
            with urllib.request.urlopen(request, context=self.ssl_context) as response:
                return response.status, self._open(response.headers, response.read())
        except urllib.error.HTTPError as err:
            # An error body is sealed too, unless the middleware rejected the
            # envelope itself — that answer predates the sealed channel.
            return err.code, self._open(err.headers, err.read())

    def _open(self, headers, payload: bytes) -> bytes:
        if not payload:
            return b""
        if (headers.get("Content-Type") or "").startswith(SEALED_MEDIA_TYPE):
            return self.session.open_response(payload)
        return payload

    def json(self, method: str, path: str, body=None):
        status, payload = self.request(
            method, path, json.dumps(body) if body is not None else None
        )
        try:
            return status, json.loads(payload) if payload else None
        except ValueError:
            return status, payload.decode("utf-8", "replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("method")
    parser.add_argument("path")
    parser.add_argument("-d", "--data", help="request body (JSON, or @file)")
    parser.add_argument("--url", default=os.environ.get("CC_AUTOMATION_URL", DEFAULT_URL))
    parser.add_argument("--key", help="API key; defaults to the server's own resolution")
    parser.add_argument("--cafile", help="pin this certificate (a self-signed cert.pem)")
    parser.add_argument(
        "--insecure", action="store_true",
        help="skip certificate verification; the envelopes still authenticate the peer",
    )
    args = parser.parse_args(argv)

    body = args.data
    if body and body.startswith("@"):
        body = Path(body[1:]).read_text(encoding="utf-8")

    client = SealedClient(args.url, args.key, cafile=args.cafile, insecure=args.insecure)
    try:
        status, payload = client.request(args.method, args.path, body)
    except SealError as exc:
        print(f"sealed reply rejected: {exc}", file=sys.stderr)
        return 2
    except urllib.error.URLError as exc:
        print(f"cannot reach {args.url}: {exc.reason}", file=sys.stderr)
        return 2

    text = payload.decode("utf-8", "replace")
    try:
        print(json.dumps(json.loads(text), indent=2))
    except ValueError:
        print(text)
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
