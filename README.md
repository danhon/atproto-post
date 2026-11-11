# atproto-post

A minimal, synchronous Bluesky PDS client for posting bots: log in, post
(with facets and image embeds), upload a blob, set the profile picture. About 250 lines of XRPC over `httpx`, no SDK.

It was extracted in 2026-11 from two bots that each had their own copy,
[Profane Town Crier](https://github.com/danhon/profane-town-crier) and
[bigpipebot](https://github.com/danhon/bigpipebot). The rules it follows are
the fleet's atproto conventions: "Bluesky clients" in `danhon/reverse-proxy`,
`docs-site/docs/services/bluesky-clients.md`.

```python
from atproto_post import AmbiguousWriteError, BlueskyClient, BlueskyError

with BlueskyClient("https://bsky.social", "bot.bsky.social", app_password,
                   langs=["en-US"]) as client:
    try:
        result = client.post("Hello", facets=None)   # PostResult(uri, cid)
    except AmbiguousWriteError:
        ...  # may have been posted: record it as done-or-unknown, never retry
    except BlueskyError:
        ...  # refused, nothing written: safe to try again next tick

    # An image post: upload first, then reference the blob in the embed.
    # Keep the ref, keyed by blob_cid(jpeg_bytes), and reuse it next time.
    blob = client.upload_blob(jpeg_bytes, "image/jpeg")
    client.post("A bird", embed={"$type": "app.bsky.embed.images",
                                 "images": [{"alt": "A bird", "image": blob}]})
```

## What it does with failures

| Response | `login` (createSession) | `post` (createRecord) | `upload_blob` |
|---|---|---|---|
| `429` with `Retry-After` | Retried, waiting that long (capped at 5 min); then `RateLimitedError` | Same | Same |
| `429` without `Retry-After` | Retried once after 1 s, then `RateLimitedError` (`retry_after=None`) | Same | Same |
| `5xx` | Retried | `AmbiguousWriteError`, not retried | Retried (a blob is harmless to upload twice) |
| Transport error (timeout, reset) | `BlueskyError` (nothing was written) | `AmbiguousWriteError` | `BlueskyError` |
| `2xx` with no readable session | `BlueskyError` | — | — |
| `2xx` with no readable `uri` | — | `AmbiguousWriteError` | — |
| `400 ExpiredToken`/`InvalidToken`, or `401` | — | One re-login, one retry | One re-login, one retry |
| Other `4xx` | `BlueskyError` | `BlueskyError` | `BlueskyError` |

**An ambiguous write is the caller's problem, deliberately.** The PDS may have
committed the record and then failed to say so. Retrying would risk a
duplicate on a public timeline, which can't be undone. Missing one post can be
recovered from. So the library never retries it, and the app has to record
that slot or row as attempted. `AmbiguousWriteError` subclasses
`BlueskyError`. If you catch it, catch it first.

**Nothing else escapes.** Every failure of `login`, `post`, `upload_blob` and
`set_avatar` is a `BlueskyError` or its subclass, so `except BlueskyError` is a
complete handler. This matters inside `post`, which logs in again by itself
when the session expires (about every two hours): a network blip during that
login is a refusal, not an ambiguous write. Before v0.2.1 it escaped as a raw
`httpx.TransportError`, which bigpipebot's ledger recorded as `unknown` and so
never posted.

**A 429 without a `Retry-After` is not retried for long.** On 2026-11-11
bsky.social refused every `uploadBlob` from BlueBirdNET for hours with a bare
429. Its `RateLimit-*` headers described the general per-IP budget (2999
of 3000 left), not the limit that was hit, so the library doesn't read them.
Retrying at 1, 2 and 4 seconds made four calls per post into a cap that
wasn't going to lift, and kept the bot pressed against it. Now a bare 429 is
retried once, in case it was a burst, and then raised as `RateLimitedError`
with `retry_after=None`. That means "not soon": back off for a long while,
for example skip pictures for an hour, rather than trying again next tick.

**Test the exception, not its message.** Every `BlueskyError` carries
`.status`, the HTTP status, or `None` for a transport error or an unreadable
reply. `RateLimitedError` (a `BlueskyError`, never ambiguous) adds
`.retry_after`, in seconds or `None`.

**Don't upload the same picture twice.** Uploading again is harmless, but
uploads are capped separately from posts. `blob_cid(content)` gives the CID
the PDS will assign (CIDv1, raw, sha256, base32): keep the ref that
`upload_blob` returns under it, and put it straight into the next record. A
record may reference a blob another record already uses. Checked live on
2026-11-11: the CIDs of BlueBirdNET's cached pictures match the blobs on its
posts.

`set_avatar` is state, not an event: setting the same picture twice is
harmless, so any failure is safe to retry on the next tick. It keeps the rest
of the profile record and uses `swapRecord`, so a hand edit made between the
read and the write is not overwritten.

## Logging

The library logs to the `atproto_post.client` logger and never configures
logging itself: no handlers, no levels. What it says:

| Level | When |
|---|---|
| `INFO` | `logged in as <handle> (<did>)`; `session expired; logging in again` |
| `WARNING` | `PDS returned <code>; retrying in <n>s (<attempt>/<max>)`, on a `429` (any call; once only without `Retry-After`) or a `5xx` (`login`, `upload_blob`) |

**It doesn't log post outcomes.** Posted, refused and ambiguous all come back
to the caller, as a `PostResult` or an exception. The app logs them, because
only the app knows which slot, detection or ledger row the post was for. If
the library logged them too, every outcome would appear twice.

**Quiet httpx in the app.** httpx logs every request at `INFO`
(`HTTP Request: POST https://bsky.social/xrpc/... "HTTP/1.1 200 OK"`), so an
app logging at `INFO` gets one extra line per call. A post with an image is
two calls. The library leaves this alone, because it would mean changing
another package's logging behind the app's back. Add this where the app sets
up logging:

```python
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
```

Leave them at `INFO` while debugging a call.

**Count degradations; don't just log them.** If a bot carries on without
something (a picture, a facet), log it, and also count it in whatever
health line the bot writes. One bot here logged a warning on every post for
two months while its pictures were missing, and nobody saw it.

## Not in scope

Async, rate limiting of AppView reads, scheduling, deduplication, templating,
and dry-run all live in the apps. That's where the bots differ.

## Versions

- `v0.3.0` (2026-11-11): `RateLimitedError` (with `retry_after`), `.status` on
  every `BlueskyError`, and `blob_cid()`. **Behaviour change:** a 429 without
  `Retry-After` is retried once, not up to `max_retries` times, then raised.
  Code that catches `BlueskyError` needs no change. For BlueBirdNET.
- `v0.2.1` (2026-11-11): every login failure (transport error, malformed reply) is a
  `BlueskyError`, including the re-login inside `post`; `Retry-After` capped at
  5 minutes; an unreadable profile in `set_avatar` is a `BlueskyError`. Fixes only.
- `v0.2.0` (2026-11-10): `upload_blob`, and `post(..., embed=...)`. Additive. For BlueBirdNET.
- `v0.1.0` (2026-11-10): extracted from bigpipebot and Profane Town Crier.

## Using it from an app

Pin a tag's tarball. The fleet's Docker build images have no `git`, so a
git dependency would fail inside `docker compose build`:

```toml
dependencies = [
    "atproto-post @ https://github.com/danhon/atproto-post/archive/refs/tags/v0.3.0.tar.gz",
]
```

`uv.lock` records the tarball's hash. **Never move or re-push a tag:**
every app pinned to it would fail its next `--frozen` build. Release a new
one instead and run `uv lock --upgrade-package atproto-post` in the apps that
want it.

To work on the library and an app together, `uv add --editable
../atproto-post` in the app, and don't commit that.

## Tests

```bash
uv run pytest -q
```

No network: every request is served by `httpx.MockTransport`.
