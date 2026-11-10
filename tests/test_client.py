"""Client tests. No network: every request is served by a fake transport."""

from __future__ import annotations

import json

import httpx
import pytest

from atproto_post import AmbiguousWriteError, BlueskyClient, BlueskyError
from atproto_post import client as client_module

SESSION = {"accessJwt": "jwt-1", "refreshJwt": "r", "did": "did:plc:abc", "handle": "pipe.test"}
CREATED = {"uri": "at://did:plc:abc/app.bsky.feed.post/1", "cid": "cid1"}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_module.time, "sleep", lambda s: None)


def _client(handler, **kwargs) -> BlueskyClient:
    kwargs.setdefault("langs", ["en-US"])
    return BlueskyClient("https://bsky.social", "pipe.test", "app-pass",
                         client=httpx.Client(transport=httpx.MockTransport(handler)), **kwargs)


def _router(create_responses):
    calls = []
    responses = iter(create_responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit(".", 1)[-1])
        if request.url.path.endswith("createSession"):
            return httpx.Response(200, json=SESSION)
        r = next(responses)
        if isinstance(r, Exception):
            raise r
        return r

    return handler, calls


def test_post_with_facets():
    seen = {}

    def handler(request):
        if request.url.path.endswith("createSession"):
            return httpx.Response(200, json=SESSION)
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=CREATED)

    facets = [{"index": {"byteStart": 0, "byteEnd": 1}, "features": []}]
    result = _client(handler).post("hi", facets)
    assert result.uri == CREATED["uri"]
    assert seen["repo"] == "did:plc:abc"
    assert seen["record"]["facets"] == facets
    assert seen["record"]["langs"] == ["en-US"]


def test_429_is_retried():
    handler, calls = _router([httpx.Response(429, headers={"retry-after": "1"}),
                              httpx.Response(200, json=CREATED)])
    _client(handler).post("hi")
    assert calls.count("createRecord") == 2


def test_5xx_on_write_is_ambiguous_and_not_retried():
    handler, calls = _router([httpx.Response(502), httpx.Response(200, json=CREATED)])
    with pytest.raises(AmbiguousWriteError):
        _client(handler).post("hi")
    assert calls.count("createRecord") == 1


def test_transport_error_on_write_is_ambiguous():
    handler, _ = _router([httpx.ReadTimeout("slow")])
    with pytest.raises(AmbiguousWriteError):
        _client(handler).post("hi")


def test_2xx_without_uri_is_ambiguous():
    handler, _ = _router([httpx.Response(200, text="not json")])
    with pytest.raises(AmbiguousWriteError):
        _client(handler).post("hi")


def test_4xx_is_a_definite_failure():
    handler, _ = _router([httpx.Response(400, json={"error": "InvalidRequest"})])
    with pytest.raises(BlueskyError) as exc:
        _client(handler).post("hi")
    assert not isinstance(exc.value, AmbiguousWriteError)


@pytest.mark.parametrize("expired", [
    httpx.Response(400, json={"error": "ExpiredToken"}),
    httpx.Response(401, json={"error": "AuthRequired"}),
])
def test_expired_token_relogs_and_retries_once(expired):
    handler, calls = _router([expired, httpx.Response(200, json=CREATED)])
    _client(handler).post("hi")
    assert calls == ["createSession", "createRecord", "createSession", "createRecord"]


def test_login_5xx_is_retried():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=SESSION)

    client = _client(handler)
    client.login()
    assert client.did == "did:plc:abc"
    assert len(attempts) == 2


# --- from Profane Town Crier's tests -----------------------------------------


def test_a_write_is_still_retried_on_429():
    """429 means the request was rejected outright, so nothing was written."""
    handler, calls = _router([httpx.Response(429), httpx.Response(429),
                              httpx.Response(200, json=CREATED)])
    assert _client(handler, max_retries=4).post("hello").cid == "cid1"
    assert calls.count("createRecord") == 3


def test_bad_credentials_are_not_retried():
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(401, json={"error": "AuthenticationRequired"})

    with pytest.raises(BlueskyError, match="createSession"):
        _client(handler).login()
    # A dead app password will not fix itself by being asked four times.
    assert len(attempts) == 1


def test_post_logs_in_only_once_across_calls():
    handler, calls = _router([httpx.Response(200, json=CREATED), httpx.Response(200, json=CREATED)])
    client = _client(handler)
    client.post("one")
    client.post("two")
    assert calls.count("createSession") == 1


def test_401_on_write_is_treated_as_an_expired_session():
    handler, calls = _router([httpx.Response(401), httpx.Response(200, json=CREATED)])
    assert _client(handler).post("hi").uri == CREATED["uri"]
    assert calls.count("createSession") == 2


# --- new in the library -------------------------------------------------------


def test_langs_are_written_to_the_record():
    seen = {}

    def handler(request):
        if request.url.path.endswith("createSession"):
            return httpx.Response(200, json=SESSION)
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=CREATED)

    _client(handler, langs=["en-GB"]).post("It's three o'clock.")
    assert seen["record"]["langs"] == ["en-GB"]
    assert seen["record"]["$type"] == "app.bsky.feed.post"
    assert seen["collection"] == "app.bsky.feed.post"


def test_langs_is_required():
    with pytest.raises(TypeError):
        BlueskyClient("https://bsky.social", "pipe.test", "app-pass")


def test_context_manager_closes_the_http_client():
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with BlueskyClient("https://bsky.social", "u", "p", langs=["en"], client=http):
        assert not http.is_closed
    assert http.is_closed
