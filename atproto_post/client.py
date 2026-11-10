"""A minimal Bluesky PDS client: createSession, createRecord and the
profile picture, no SDK.

Follows the fleet's atproto conventions (reverse-proxy
docs-site/docs/services/bluesky-clients.md):

- `429` is retried, honouring Retry-After. `5xx` is retried only on
  createSession; on createRecord it is **ambiguous** and never repeated.
- A transport error on createRecord, or a `2xx` with no readable `uri`, is
  ambiguous too. All three raise `AmbiguousWriteError`, and the caller leaves
  its ledger row `unknown` so the post is never attempted again.
- `ExpiredToken` can arrive as `400`, not `401`; both trigger one re-login and
  one retry, which is safe because the PDS refused before writing.

Extracted from bigpipebot and Profane Town Crier, 2026-11. Sync only: speedcam's
async client is deliberately not served by this (see the README).
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

POST_COLLECTION = "app.bsky.feed.post"
PROFILE_COLLECTION = "app.bsky.actor.profile"


class BlueskyError(RuntimeError):
    """A PDS call was refused. Nothing was written."""


class AmbiguousWriteError(BlueskyError):
    """A write may or may not have happened. Never retry it."""


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

    def _post_with_backoff(self, url: str, payload: dict, headers: dict | None = None, *,
                           retry_server_errors: bool) -> httpx.Response:
        delay = 1.0
        response: httpx.Response | None = None
        for attempt in range(self.max_retries):
            response = self._client.post(url, json=payload, headers=headers or {})
            code = response.status_code
            retryable = code == 429 or (code >= 500 and retry_server_errors)
            if not retryable or attempt == self.max_retries - 1:
                return response
            wait = delay
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    wait = max(wait, float(retry_after))
                except ValueError:
                    pass
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
            return response.json().get("error") in {"ExpiredToken", "InvalidToken"}
        except ValueError:
            return False

    def login(self) -> None:
        response = self._post_with_backoff(
            self._url("com.atproto.server.createSession"),
            {"identifier": self.username, "password": self.app_password},
            retry_server_errors=True,
        )
        if response.status_code >= 400:
            raise BlueskyError(
                f"createSession failed: HTTP {response.status_code} {response.text[:300]}"
            )
        data = response.json()
        self._access_jwt = data["accessJwt"]
        self.did = data["did"]
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
             created_at: dt.datetime | None = None) -> PostResult:
        """`created_at` backdates the record (backfill only); default now."""
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
        payload = {"repo": self.did, "collection": POST_COLLECTION, "record": record}

        response = self._create(payload)
        if self._is_expired(response):
            log.info("session expired; logging in again")
            self.login()
            payload["repo"] = self.did
            response = self._create(payload)

        code = response.status_code
        if code >= 500:
            raise AmbiguousWriteError(f"createRecord HTTP {code}: {response.text[:300]}")
        if code >= 400:
            raise BlueskyError(f"createRecord HTTP {code}: {response.text[:300]}")
        try:
            data = response.json()
            uri = data["uri"]
        except (ValueError, KeyError, TypeError) as exc:
            raise AmbiguousWriteError(f"createRecord {code} without a uri: {response.text[:300]}") from exc
        return PostResult(uri=uri, cid=data.get("cid"))

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
        r = self._authed(lambda h: self._client.post(
            self._url("com.atproto.repo.uploadBlob"), content=png,
            headers={**h, "Content-Type": "image/png"}))
        if r.status_code >= 400:
            raise BlueskyError(f"uploadBlob HTTP {r.status_code}: {r.text[:300]}")
        blob = r.json()["blob"]

        r = self._authed(lambda h: self._client.get(
            self._url("com.atproto.repo.getRecord"), headers=h,
            params={"repo": self.did, "collection": PROFILE_COLLECTION, "rkey": "self"}))
        if r.status_code == 400 and "RecordNotFound" in r.text:
            record, swap = {"$type": PROFILE_COLLECTION}, None
        elif r.status_code >= 400:
            raise BlueskyError(f"getRecord profile HTTP {r.status_code}: {r.text[:300]}")
        else:
            data = r.json()
            record, swap = data["value"], data.get("cid")

        record["avatar"] = blob
        payload = {"repo": self.did, "collection": PROFILE_COLLECTION, "rkey": "self",
                   "record": record}
        if swap:
            # Refuse to write if the profile changed since we read it (say,
            # someone edited the bio by hand): next tick reads it afresh.
            payload["swapRecord"] = swap
        r = self._authed(lambda h: self._client.post(
            self._url("com.atproto.repo.putRecord"), json=payload, headers=h))
        if r.status_code >= 400:
            raise BlueskyError(f"putRecord profile HTTP {r.status_code}: {r.text[:300]}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> BlueskyClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
