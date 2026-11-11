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


# --- set_avatar (from bigpipebot's tests/test_avatar.py) ----------------------

BLOB = {"$type": "blob", "ref": {"$link": "bafk"}, "mimeType": "image/png", "size": 9}


def _profile_handler(get_response):
    seen = {}

    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        if method.endswith("createSession"):
            return httpx.Response(200, json=SESSION)
        if method.endswith("uploadBlob"):
            seen["upload_type"] = request.headers["content-type"]
            return httpx.Response(200, json={"blob": BLOB})
        if method.endswith("getRecord"):
            return get_response
        if method.endswith("putRecord"):
            seen["put"] = json.loads(request.content)
            return httpx.Response(200, json={"uri": "at://x", "cid": "c2"})
        raise AssertionError(method)

    return handler, seen


def test_set_avatar_keeps_the_rest_of_the_profile():
    existing = {"$type": "app.bsky.actor.profile", "displayName": "Big Pipe",
                "description": "bio", "avatar": {"old": True}}
    handler, seen = _profile_handler(httpx.Response(200, json={"value": existing, "cid": "c1"}))
    _client(handler).set_avatar(b"png")
    put = seen["put"]
    assert seen["upload_type"] == "image/png"
    assert put["rkey"] == "self" and put["swapRecord"] == "c1"
    assert put["record"]["displayName"] == "Big Pipe"
    assert put["record"]["description"] == "bio"
    assert put["record"]["avatar"] == BLOB


def test_set_avatar_creates_a_missing_profile():
    handler, seen = _profile_handler(httpx.Response(400, json={"error": "RecordNotFound"}))
    _client(handler).set_avatar(b"png")
    assert "swapRecord" not in seen["put"]
    assert seen["put"]["record"]["avatar"] == BLOB


def test_set_avatar_refusal_raises():
    handler, _ = _profile_handler(httpx.Response(500))
    with pytest.raises(BlueskyError):
        _client(handler).set_avatar(b"png")


# --- upload_blob and embeds (v0.2.0, for BlueBirdNET) -------------------------


def _blob_handler(responses):
    seen = []
    responses = iter(responses)

    def handler(request):
        if request.url.path.endswith("createSession"):
            return httpx.Response(200, json=SESSION)
        assert request.url.path.endswith("uploadBlob")
        seen.append((request.headers["content-type"], request.content,
                     request.headers["authorization"]))
        return next(responses)

    return handler, seen


def test_upload_blob_returns_the_blob():
    handler, seen = _blob_handler([httpx.Response(200, json={"blob": BLOB})])
    assert _client(handler).upload_blob(b"jpegbytes", "image/jpeg; charset=binary") == BLOB
    assert seen == [("image/jpeg", b"jpegbytes", "Bearer jwt-1")]


def test_upload_blob_retries_429_and_5xx():
    """A blob is content-addressed and unreferenced until a record uses it, so
    uploading it twice is harmless: unlike createRecord, 5xx is retried."""
    handler, seen = _blob_handler([httpx.Response(429), httpx.Response(502),
                                   httpx.Response(200, json={"blob": BLOB})])
    assert _client(handler).upload_blob(b"x", "image/png") == BLOB
    assert len(seen) == 3


def test_upload_blob_relogs_on_expired_token():
    handler, seen = _blob_handler([httpx.Response(400, json={"error": "ExpiredToken"}),
                                   httpx.Response(200, json={"blob": BLOB})])
    assert _client(handler).upload_blob(b"x", "image/png") == BLOB
    assert len(seen) == 2


def test_upload_blob_refusal_and_transport_error_raise_blueskyerror():
    handler, _ = _blob_handler([httpx.Response(413, json={"error": "BlobTooLarge"})])
    with pytest.raises(BlueskyError):
        _client(handler).upload_blob(b"x", "image/png")

    def boom(request):
        if request.url.path.endswith("createSession"):
            return httpx.Response(200, json=SESSION)
        raise httpx.ConnectError("down")

    with pytest.raises(BlueskyError):
        _client(boom).upload_blob(b"x", "image/png")


def test_post_with_embed():
    seen = {}

    def handler(request):
        if request.url.path.endswith("createSession"):
            return httpx.Response(200, json=SESSION)
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=CREATED)

    embed = {"$type": "app.bsky.embed.images", "images": [{"alt": "a bird", "image": BLOB}]}
    _client(handler).post("Anna's Hummingbird", embed=embed)
    assert seen["record"]["embed"] == embed


def test_post_without_embed_has_no_embed_key():
    seen = {}

    def handler(request):
        if request.url.path.endswith("createSession"):
            return httpx.Response(200, json=SESSION)
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=CREATED)

    _client(handler).post("hi")
    assert "embed" not in seen["record"]


# --- login failures never escape as anything but BlueskyError ----------------
# (v0.2.1) Nothing is written by createSession, so every login failure is a
# definite one: the caller's "refused, try again next tick" branch.

def _login_fails_with(exc_or_response):
    calls = []

    def handler(request):
        calls.append(request.url.path.rsplit(".", 1)[-1])
        if request.url.path.endswith("createSession"):
            if isinstance(exc_or_response, Exception):
                raise exc_or_response
            return exc_or_response
        return httpx.Response(200, json=CREATED)

    return handler, calls


def test_login_transport_error_is_a_blueskyerror_not_ambiguous():
    handler, _ = _login_fails_with(httpx.ConnectError("dns"))
    with pytest.raises(BlueskyError) as info:
        _client(handler).login()
    assert not isinstance(info.value, AmbiguousWriteError)


def test_post_whose_login_has_a_transport_error_wrote_nothing():
    handler, calls = _login_fails_with(httpx.ReadTimeout("slow"))
    with pytest.raises(BlueskyError) as info:
        _client(handler).post("hi")
    assert not isinstance(info.value, AmbiguousWriteError)
    assert "createRecord" not in calls


def test_relogin_after_expiry_with_a_transport_error_is_a_blueskyerror():
    state = {"logins": 0}

    def handler(request):
        if request.url.path.endswith("createSession"):
            state["logins"] += 1
            if state["logins"] > 1:
                raise httpx.ConnectError("dns")
            return httpx.Response(200, json=SESSION)
        return httpx.Response(400, json={"error": "ExpiredToken"})

    with pytest.raises(BlueskyError) as info:
        _client(handler).post("hi")
    assert not isinstance(info.value, AmbiguousWriteError)


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"did": "did:plc:abc"}),    # no accessJwt
    httpx.Response(200, content=b"<html>proxy</html>"),  # not JSON
    httpx.Response(200, json=["a", "list"]),             # not an object
])
def test_malformed_login_reply_is_a_blueskyerror(response):
    handler, _ = _login_fails_with(response)
    with pytest.raises(BlueskyError):
        _client(handler).login()


def test_retry_after_wait_is_capped(monkeypatch):
    slept = []
    monkeypatch.setattr(client_module.time, "sleep", slept.append)
    handler, _ = _router([httpx.Response(429, headers={"retry-after": "86400"}),
                          httpx.Response(200, json=CREATED)])
    _client(handler).post("hi")
    assert slept and max(slept) <= client_module.MAX_RETRY_WAIT_SECONDS


def test_expired_check_survives_a_non_object_body():
    def handler(request):
        if request.url.path.endswith("createSession"):
            return httpx.Response(200, json=SESSION)
        return httpx.Response(400, json=["odd"])

    with pytest.raises(BlueskyError):
        _client(handler).post("hi")


def test_set_avatar_with_an_unreadable_profile_is_a_blueskyerror():
    handler, seen = _profile_handler(httpx.Response(200, content=b"<html>"))
    with pytest.raises(BlueskyError):
        _client(handler).set_avatar(b"png")
    assert "put" not in seen
