"""A minimal Bluesky PDS client: createSession, createRecord, uploadBlob and
the profile picture, no SDK.

Follows the fleet's atproto conventions (reverse-proxy
docs-site/docs/services/bluesky-clients.md):

- `429` with a Retry-After is retried, honouring it. A `429` without one is
  retried once and then raised as `RateLimitedError`: a cap that gives no wait
  usually lifts in hours, not seconds. `5xx` is retried only on createSession
  and uploadBlob; on createRecord it is **ambiguous** and never repeated.
- A transport error on createRecord, or a `2xx` with no readable `uri`, is
  ambiguous too. All three raise `AmbiguousWriteError`, and the caller leaves
  its ledger row `unknown` so the post is never attempted again.
- `ExpiredToken` can arrive as `400`, not `401`; both trigger one re-login and
  one retry, which is safe because the PDS refused before writing.

Extracted from bigpipebot and Profane Town Crier, 2026-11. Sync only: speedcam's
async client is deliberately not served by this (see the README).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

POST_COLLECTION = "app.bsky.feed.post"
PROFILE_COLLECTION = "app.bsky.actor.profile"

# A `Retry-After` is honoured up to this long. A bot blocked for an hour on
# one call is worse than giving up and letting the next tick try again.
MAX_RETRY_WAIT_SECONDS = 300.0


class BlueskyError(RuntimeError):
    """A PDS call was refused. Nothing was written.

    `status` is the HTTP status, or None for a transport error or an
    unreadable reply, so callers can test it instead of the message."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class AmbiguousWriteError(BlueskyError):
    """A write may or may not have happened. Never retry it."""


class RateLimitedError(BlueskyError):
    """The PDS answered 429 and the retries ran out. Nothing was written.

    `retry_after` is the server's Retry-After in seconds, or None when it gave
    none. None is the common case for a per-endpoint or daily cap, and it means
    "not soon": back off for a long while (BlueBirdNET pauses uploads for an
    hour), don't retry on the next tick. RateLimit-* headers are not used: on
    bsky.social's uploadBlob 429 they described the general per-IP budget,
    not the limit that was hit."""

    def __init__(self, message: str, *, retry_after: float | None = None,
                 status: int | None = 429):
        super().__init__(message, status=status)
        self.retry_after = retry_after


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _raise_for(response: httpx.Response, what: str, *, ambiguous_5xx: bool = False) -> None:
    """Raise the right error for a 4xx/5xx `response`; return on success."""
    code = response.status_code
    if code < 400:
        return
    detail = f"{what} HTTP {code}: {response.text[:300]}"
    if code == 429:
        raise RateLimitedError(detail, retry_after=_retry_after(response))
    if code >= 500 and ambiguous_5xx:
        raise AmbiguousWriteError(detail, status=code)
    raise BlueskyError(detail, status=code)


def blob_cid(content: bytes) -> str:
    """The CID a PDS gives `content` as a blob: CIDv1, raw codec, sha256,
    base32. Use it to recognise a blob already uploaded, and reuse its ref
    instead of uploading again (see `upload_blob`)."""
    digest = hashlib.sha256(content).digest()
    raw = bytes([0x01, 0x55, 0x12, 0x20]) + digest
    return "b" + base64.b32encode(raw).decode("ascii").lower().rstrip("=")


@dataclass
class PostResult:
    uri: str
    cid: str | None


class BlueskyClient:
    def __init__(self, pds: str, username: str, app_password: str, *, langs: list[str],
                 timeout: float = 30.0, max_retries: int = 4,
                 client: httpx.Client | None = None):
        """`langs` has no default on purpose: the bots differ (en-GB, en-US), and
        a default would be silently wrong for one of them."""
        self.pds = pds.rstrip("/")
        self.langs = list(langs)
        self.username = username
        self.app_password = app_password
        self.max_retries = max_retries
        self._client = client or httpx.Client(timeout=timeout)
        self._access_jwt: str | None = None
        self.did: str | None = None
        self.handle: str | None = None

    def _url(self, method: str) -> str:
        return f"{self.pds}/xrpc/{method}"

    def _post_with_backoff(self, url: str, payload: dict | None, headers: dict | None = None, *,
                           retry_server_errors: bool, content: bytes | None = None
                           ) -> httpx.Response:
        """POST `payload` as JSON, or raw `content` when given.

        A 429 with Retry-After is retried, waiting that long (capped). A bare
        429 is retried once, after a second, in case it was a burst limit;
        a second bare 429 is returned for the caller to raise."""
        delay = 1.0
        bare_429_retried = False
        response: httpx.Response | None = None
        for attempt in range(self.max_retries):
            if content is not None:
                response = self._client.post(url, content=content, headers=headers or {})
            else:
                response = self._client.post(url, json=payload, headers=headers or {})
            code = response.status_code
            retryable = code == 429 or (code >= 500 and retry_server_errors)
            if not retryable or attempt == self.max_retries - 1:
                return response
            wait = delay
            if code == 429:
                retry_after = _retry_after(response)
                if retry_after is not None:
                    wait = min(max(wait, retry_after), MAX_RETRY_WAIT_SECONDS)
                elif bare_429_retried:
                    return response
                else:
                    bare_429_retried = True
                    wait = 1.0
            log.warning("PDS returned %s; retrying in %.1fs (%d/%d)", code, wait,
                        attempt + 1, self.max_retries)
            time.sleep(wait)
            delay = min(delay * 2, 60.0)
        assert response is not None
        return response

    @staticmethod
    def _is_expired(response: httpx.Response) -> bool:
        if response.status_code == 401:
            return True
        if response.status_code != 400:
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        return isinstance(body, dict) and body.get("error") in {"ExpiredToken", "InvalidToken"}

    def login(self) -> None:
        """Start a session. Every failure raises `BlueskyError`, never
        `AmbiguousWriteError` or an httpx exception: createSession writes
        nothing, so it is always safe to try again (from v0.2.1)."""
        try:
            response = self._post_with_backoff(
                self._url("com.atproto.server.createSession"),
                {"identifier": self.username, "password": self.app_password},
                retry_server_errors=True,
            )
        except httpx.TransportError as exc:
            raise BlueskyError(f"createSession transport error: {exc}") from exc
        _raise_for(response, "createSession")
        try:
            data = response.json()
            access_jwt, did = data["accessJwt"], data["did"]
        except (ValueError, KeyError, TypeError) as exc:
            raise BlueskyError(
                f"createSession {response.status_code} without a session: {response.text[:300]}",
                status=response.status_code,
            ) from exc
        self._access_jwt = access_jwt
        self.did = did
        self.handle = data.get("handle", self.username)
        log.info("logged in as %s (%s)", self.handle, self.did)

    def _create(self, payload: dict) -> httpx.Response:
        try:
            return self._post_with_backoff(
                self._url("com.atproto.repo.createRecord"),
                payload,
                {"Authorization": f"Bearer {self._access_jwt}"},
                retry_server_errors=False,
            )
        except httpx.TransportError as exc:
            raise AmbiguousWriteError(f"createRecord transport error: {exc}") from exc

    def post(self, text: str, facets: list[dict] | None = None, *,
             created_at: dt.datetime | None = None, embed: dict | None = None) -> PostResult:
        """`created_at` backdates the record (backfill only); default now.
        `embed` is passed through as the record's embed, e.g. an
        `app.bsky.embed.images` holding blobs from `upload_blob`."""
        if self._access_jwt is None:
            self.login()
        when = (created_at or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
        record = {
            "$type": POST_COLLECTION,
            "text": text,
            "createdAt": when.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "langs": self.langs,
        }
        if facets:
            record["facets"] = facets
        if embed is not None:
            record["embed"] = embed
        payload = {"repo": self.did, "collection": POST_COLLECTION, "record": record}

        response = self._create(payload)
        if self._is_expired(response):
            log.info("session expired; logging in again")
            self.login()
            payload["repo"] = self.did
            response = self._create(payload)

        code = response.status_code
        _raise_for(response, "createRecord", ambiguous_5xx=True)
        try:
            data = response.json()
            uri = data["uri"]
        except (ValueError, KeyError, TypeError) as exc:
            raise AmbiguousWriteError(f"createRecord {code} without a uri: {response.text[:300]}",
                                      status=code) from exc
        return PostResult(uri=uri, cid=data.get("cid"))

    # --- blobs ----------------------------------------------------------------

    def upload_blob(self, content: bytes, content_type: str) -> dict:
        """Upload `content` and return the blob reference to put in a record.

        Unlike createRecord, this retries `5xx` too: a blob is content-addressed
        and stays unreferenced (and is eventually discarded) until a record
        points at it, so uploading it twice is harmless. Any failure raises
        `BlueskyError` (`RateLimitedError` on a 429), never `AmbiguousWriteError`.

        Harmless, but not free: bsky.social caps uploads separately from posts.
        A bot that posts the same picture again should keep the ref this
        returns, keyed by `blob_cid(content)`, and put it in the new record
        instead of uploading again. A record may reference a blob that another
        record already uses."""
        mime = content_type.split(";", 1)[0].strip() or "application/octet-stream"
        if self._access_jwt is None:
            self.login()

        def send() -> httpx.Response:
            return self._post_with_backoff(
                self._url("com.atproto.repo.uploadBlob"), None,
                {"Authorization": f"Bearer {self._access_jwt}", "Content-Type": mime},
                retry_server_errors=True, content=content)

        try:
            response = send()
            if self._is_expired(response):
                log.info("session expired; logging in again")
                self.login()
                response = send()
        except httpx.TransportError as exc:
            raise BlueskyError(f"uploadBlob transport error: {exc}") from exc
        _raise_for(response, "uploadBlob")
        try:
            return response.json()["blob"]
        except (ValueError, KeyError, TypeError) as exc:
            raise BlueskyError(f"uploadBlob without a blob: {response.text[:300]}",
                               status=response.status_code) from exc

    # --- profile picture -----------------------------------------------------
    #
    # Unlike a post, the avatar is state, not an event: setting it twice to
    # the same picture is harmless. So these calls are simply retried (by the
    # caller, next tick) after any failure, ambiguous or not.

    def _authed(self, send) -> httpx.Response:
        if self._access_jwt is None:
            self.login()
        try:
            response = send({"Authorization": f"Bearer {self._access_jwt}"})
            if self._is_expired(response):
                log.info("session expired; logging in again")
                self.login()
                response = send({"Authorization": f"Bearer {self._access_jwt}"})
        except httpx.TransportError as exc:
            raise BlueskyError(f"transport error: {exc}") from exc
        return response

    def set_avatar(self, png: bytes) -> None:
        """Upload `png` and make it the profile picture, keeping every other
        field of the profile record (name, bio, banner) as it is."""
        blob = self.upload_blob(png, "image/png")

        r = self._authed(lambda h: self._client.get(
            self._url("com.atproto.repo.getRecord"), headers=h,
            params={"repo": self.did, "collection": PROFILE_COLLECTION, "rkey": "self"}))
        if r.status_code == 400 and "RecordNotFound" in r.text:
            record, swap = {"$type": PROFILE_COLLECTION}, None
        elif r.status_code >= 400:
            _raise_for(r, "getRecord profile")
        else:
            try:
                data = r.json()
                record, swap = dict(data["value"]), data.get("cid")
            except (ValueError, KeyError, TypeError) as exc:
                raise BlueskyError(f"getRecord profile unreadable: {r.text[:300]}") from exc

        record["avatar"] = blob
        payload = {"repo": self.did, "collection": PROFILE_COLLECTION, "rkey": "self",
                   "record": record}
        if swap:
            # Refuse to write if the profile changed since we read it (say,
            # someone edited the bio by hand): next tick reads it afresh.
            payload["swapRecord"] = swap
        r = self._authed(lambda h: self._client.post(
            self._url("com.atproto.repo.putRecord"), json=payload, headers=h))
        _raise_for(r, "putRecord profile")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> BlueskyClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
