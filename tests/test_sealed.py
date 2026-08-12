"""The sealed transport: the crypto primitives, then the middleware end to end.

No `claude` subprocess is ever spawned here — workers are off, so an accepted
message sits in its queue where a test can look at it.
"""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.main import SEALED_MEDIA_TYPE, Config, create_app
from server.sealed import (
    AAD_REQUEST,
    AAD_RESPONSE,
    AUTH_HEADER,
    NONCE_BYTES,
    SEALED_HEADER,
    VERSION,
    Claims,
    NonceGuard,
    SealError,
    SealedSession,
    derive_key,
    seal,
    seal_claims,
    unseal,
)

KEY = "test-key-abcdefghijklmnop"

AGENTS_YAML = """
agents:
  - name: alpha
    tags: [research, shared]
    cwd: {cwd}
    allowed_tools: [Read]
    permission_mode: bypassPermissions
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "agents.yaml").write_text(AGENTS_YAML.format(cwd=work))
    return tmp_path


def build(home: Path, **kw):
    config = Config(home=home, api_key=KEY, start_workers=False, claude_bin="claude", **kw)
    return TestClient(create_app(config))


@pytest.fixture
def client(home: Path):
    with build(home) as c:
        yield c


@pytest.fixture
def session() -> SealedSession:
    return SealedSession(KEY)


def call(client, session, method: str, path: str, body=None):
    """One sealed round trip -> `(status, plaintext text)`."""
    raw = json.dumps(body).encode() if body is not None else None
    auth, sealed_body = session.seal_request(method, path, raw, "application/json")
    headers = {SEALED_HEADER: VERSION, AUTH_HEADER: auth}
    if sealed_body:
        headers["Content-Type"] = SEALED_MEDIA_TYPE
    response = client.request(method, path, content=sealed_body, headers=headers)
    if not response.content:
        return response.status_code, ""
    if response.headers.get("content-type", "").startswith(SEALED_MEDIA_TYPE):
        return response.status_code, session.open_response(response.content).decode()
    return response.status_code, response.text


# -- primitives -------------------------------------------------------------


def test_derivation_is_deterministic_and_key_specific():
    assert derive_key(KEY) == derive_key(KEY)
    assert derive_key(KEY) != derive_key(KEY + "x")
    assert len(derive_key(KEY)) == 32


def test_seal_round_trips():
    key = derive_key(KEY)
    envelope = seal(key, b"hello", AAD_REQUEST)
    assert envelope.startswith(f"{VERSION}.")
    assert unseal(key, envelope, AAD_REQUEST) == b"hello"


def test_envelope_hides_the_plaintext():
    key = derive_key(KEY)
    assert b"summarise today" not in seal(key, b"summarise today", AAD_REQUEST).encode()


def test_nonce_differs_per_envelope():
    key = derive_key(KEY)
    assert seal(key, b"same", AAD_REQUEST) != seal(key, b"same", AAD_REQUEST)


def test_a_flipped_byte_does_not_verify():
    key = derive_key(KEY)
    envelope = seal(key, b"hello", AAD_REQUEST)
    head, nonce, ciphertext = envelope.split(".")
    tampered = f"{head}.{nonce}.{'A' if ciphertext[0] != 'A' else 'B'}{ciphertext[1:]}"
    with pytest.raises(SealError):
        unseal(key, tampered, AAD_REQUEST)


def test_the_wrong_key_does_not_verify():
    envelope = seal(derive_key(KEY), b"hello", AAD_REQUEST)
    with pytest.raises(SealError):
        unseal(derive_key("another-key"), envelope, AAD_REQUEST)


def test_aad_separates_the_positions():
    """A response envelope must not verify where a request is expected."""
    key = derive_key(KEY)
    envelope = seal(key, b"hello", AAD_RESPONSE)
    with pytest.raises(SealError):
        unseal(key, envelope, AAD_REQUEST)


@pytest.mark.parametrize(
    "envelope",
    ["", "junk", "v1.only-two", "v2.aaaa.bbbb", "v1.!!!.bbbb", "v1..bbbb"],
)
def test_malformed_envelopes_are_refused(envelope):
    with pytest.raises(SealError):
        unseal(derive_key(KEY), envelope, AAD_REQUEST)


def test_short_nonce_is_refused():
    key = derive_key(KEY)
    _, _, ciphertext = seal(key, b"hello", AAD_REQUEST).split(".")
    with pytest.raises(SealError):
        unseal(key, f"{VERSION}.AAAA.{ciphertext}", AAD_REQUEST)


# -- replay -----------------------------------------------------------------


def test_a_nonce_is_accepted_once():
    guard = NonceGuard()
    now = time.time()
    guard.check(b"n" * NONCE_BYTES, int(now), now=now)
    with pytest.raises(SealError):
        guard.check(b"n" * NONCE_BYTES, int(now), now=now)


def test_a_stale_timestamp_is_refused():
    guard = NonceGuard(skew_s=300)
    now = time.time()
    with pytest.raises(SealError):
        guard.check(b"n" * NONCE_BYTES, int(now - 301), now=now)


def test_a_future_timestamp_is_refused():
    guard = NonceGuard(skew_s=300)
    now = time.time()
    with pytest.raises(SealError):
        guard.check(b"n" * NONCE_BYTES, int(now + 301), now=now)


def test_the_cache_is_bounded():
    guard = NonceGuard(capacity=4)
    now = time.time()
    for i in range(10):
        guard.check(bytes([i]) * NONCE_BYTES, int(now), now=now)
    assert len(guard._seen) <= 4


def test_expired_nonces_leave_the_cache():
    guard = NonceGuard(skew_s=10)
    guard.check(b"n" * NONCE_BYTES, int(1000), now=1000.0)
    # Far enough on that the old entry is evicted rather than remembered.
    guard.check(b"m" * NONCE_BYTES, int(2000), now=2000.0)
    assert b"n" * NONCE_BYTES not in guard._seen


# -- claims -----------------------------------------------------------------


def test_claims_bind_the_method(session):
    auth, _ = session.seal_request("GET", "/agents", None, None)
    with pytest.raises(SealError):
        session.open_request("DELETE", "/agents", auth, b"")


def test_claims_bind_the_path(session):
    auth, _ = session.seal_request("GET", "/agents", None, None)
    with pytest.raises(SealError):
        session.open_request("GET", "/projects", auth, b"")


def test_claims_bind_the_query_string(session):
    auth, _ = session.seal_request("GET", "/tasks?status=failed", None, None)
    with pytest.raises(SealError):
        session.open_request("GET", "/tasks?status=running", auth, b"")


def test_claims_bind_the_body(session):
    auth, _ = session.seal_request("POST", "/messages", b'{"a":1}', "application/json")
    substituted = seal(session.key, b'{"a":2}', AAD_REQUEST).encode()
    with pytest.raises(SealError):
        session.open_request("POST", "/messages", auth, substituted)


def test_the_matching_body_opens(session):
    """The counterpart to the test above: the binding is what refused it, not
    the key. A second envelope, because the first burned its nonce."""
    auth, body = session.seal_request("POST", "/messages", b'{"a":1}', "application/json")
    assert session.open_request("POST", "/messages", auth, body)[0] == b'{"a":1}'


def test_a_rejected_request_still_burns_its_nonce(session):
    """Freshness is checked before the bindings, so a failed attempt cannot be
    retried with a corrected body. Fail closed: an envelope is used once,
    whatever the outcome."""
    auth, body = session.seal_request("POST", "/messages", b'{"a":1}', "application/json")
    with pytest.raises(SealError):
        session.open_request("POST", "/wrong-path", auth, body)
    with pytest.raises(SealError):
        session.open_request("POST", "/messages", auth, body)


def test_a_body_without_a_claim_is_refused(session):
    auth, _ = session.seal_request("POST", "/messages", None, None)
    smuggled = seal(session.key, b'{"a":1}', AAD_REQUEST).encode()
    with pytest.raises(SealError):
        session.open_request("POST", "/messages", auth, smuggled)


def test_a_claimed_body_that_is_missing_is_refused(session):
    auth, _ = session.seal_request("POST", "/messages", b'{"a":1}', "application/json")
    with pytest.raises(SealError):
        session.open_request("POST", "/messages", auth, b"")


def test_claims_that_are_not_json_are_refused(session):
    with pytest.raises(SealError):
        session.open_request("GET", "/agents", seal(session.key, b"nope", b"cc-automation/sealed/v1/auth"), b"")


def test_claims_with_a_wrong_shape_are_refused(session):
    envelope = seal(
        session.key, json.dumps({"m": "GET", "p": "/agents"}).encode(),
        b"cc-automation/sealed/v1/auth",
    )
    with pytest.raises(SealError):
        session.open_request("GET", "/agents", envelope, b"")


# -- middleware, end to end -------------------------------------------------


def test_a_sealed_get_is_served(client, session):
    status, text = call(client, session, "GET", "/agents")
    assert status == 200
    assert [a["name"] for a in json.loads(text)] == ["alpha"]


def test_the_key_never_appears_on_the_wire(client, session):
    """The whole point: no request carries the credential."""
    auth, body = session.seal_request(
        "POST", "/messages", json.dumps({"text": "secret words", "tags": ["research"]}).encode(),
        "application/json",
    )
    assert KEY not in auth
    assert KEY.encode() not in body
    assert b"secret words" not in body


def test_a_sealed_post_reaches_the_route(client, session):
    status, text = call(
        client, session, "POST", "/messages",
        {"text": "summarise today", "tags": ["research"], "topic": "daily"},
    )
    assert status == 202
    queue = client.app.state.cc.dispatcher.queue("alpha")
    assert [job.text for job in list(queue._queue)] == ["summarise today"]


def test_the_response_body_is_ciphertext(client, session):
    auth, _ = session.seal_request("GET", "/agents", None, None)
    raw = client.get("/agents", headers={SEALED_HEADER: VERSION, AUTH_HEADER: auth})
    assert raw.headers["content-type"].startswith(SEALED_MEDIA_TYPE)
    assert b"alpha" not in raw.content
    assert json.loads(session.open_response(raw.content))[0]["name"] == "alpha"


def test_the_inner_content_type_is_reported(client, session):
    auth, _ = session.seal_request("GET", "/agents", None, None)
    raw = client.get("/agents", headers={SEALED_HEADER: VERSION, AUTH_HEADER: auth})
    assert raw.headers["X-CC-Type"].startswith("application/json")


def test_a_sealed_error_is_still_sealed(client, session):
    status, text = call(client, session, "GET", "/tasks/does-not-exist")
    assert status == 404
    assert "detail" in json.loads(text)


def test_a_forged_envelope_is_rejected(client):
    forged = SealedSession("not-the-key")
    auth, _ = forged.seal_request("GET", "/agents", None, None)
    response = client.get("/agents", headers={SEALED_HEADER: VERSION, AUTH_HEADER: auth})
    assert response.status_code == 401


def test_a_sealed_request_without_an_envelope_is_rejected(client):
    assert client.get("/agents", headers={SEALED_HEADER: VERSION}).status_code == 401


def test_a_replayed_request_is_rejected(client, session):
    auth, _ = session.seal_request("GET", "/agents", None, None)
    headers = {SEALED_HEADER: VERSION, AUTH_HEADER: auth}
    assert client.get("/agents", headers=headers).status_code == 200
    assert client.get("/agents", headers=headers).status_code == 401


def test_an_envelope_replayed_onto_another_route_is_rejected(client, session):
    auth, _ = session.seal_request("GET", "/agents", None, None)
    response = client.get("/projects", headers={SEALED_HEADER: VERSION, AUTH_HEADER: auth})
    assert response.status_code == 401


def test_a_stale_envelope_is_rejected(client, session):
    claims = Claims(method="GET", path="/agents", ts=int(time.time()) - 3600)
    auth = seal_claims(session.key, claims)
    response = client.get("/agents", headers={SEALED_HEADER: VERSION, AUTH_HEADER: auth})
    assert response.status_code == 401


def test_rejection_says_nothing_about_which_check_failed(client, session):
    """Bad key and stale clock must be indistinguishable to the caller."""
    forged = SealedSession("not-the-key")
    bad_key, _ = forged.seal_request("GET", "/agents", None, None)
    stale = seal_claims(session.key, Claims("GET", "/agents", int(time.time()) - 3600))
    answers = {
        client.get("/agents", headers={SEALED_HEADER: VERSION, AUTH_HEADER: env}).text
        for env in (bad_key, stale)
    }
    assert len(answers) == 1


# -- coexistence with the plaintext path ------------------------------------


def test_plaintext_still_works_by_default(client):
    assert client.get("/agents", headers={"X-API-Key": KEY}).status_code == 200


def test_health_is_still_open(client):
    assert client.get("/health").status_code == 200


def test_require_sealed_refuses_plaintext(home):
    with build(home, require_sealed=True) as c:
        response = c.get("/agents", headers={"X-API-Key": KEY})
        assert response.status_code == 426
        assert "sealed" in response.json()["detail"]


def test_require_sealed_still_serves_sealed(home):
    with build(home, require_sealed=True) as c:
        status, text = call(c, SealedSession(KEY), "GET", "/agents")
        assert status == 200
        assert [a["name"] for a in json.loads(text)] == ["alpha"]


def test_require_sealed_leaves_health_open(home):
    with build(home, require_sealed=True) as c:
        assert c.get("/health").status_code == 200


def test_require_sealed_leaves_the_console_reachable(home):
    """The shell has to load before the browser can seal anything."""
    with build(home, require_sealed=True) as c:
        assert c.get("/web/index.html").status_code == 200


def test_require_sealed_is_off_unless_asked(home):
    with build(home) as c:
        assert c.app.state.cc.config.require_sealed is False


# -- the reference client ---------------------------------------------------


def test_the_client_and_the_server_agree_on_framing(session):
    """`clients/sealed_client.py` must frame what `server/sealed.py` opens."""
    from clients.sealed_client import SealedClient

    client = SealedClient(api_key=KEY)
    auth, body = client.session.seal_request(
        "POST", "/messages", b'{"text":"hi"}', "application/json"
    )
    plain, content_type = SealedSession(KEY).open_request("POST", "/messages", auth, body)
    assert plain == b'{"text":"hi"}'
    assert content_type == "application/json"
