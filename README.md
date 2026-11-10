# atproto-post

A minimal, synchronous Bluesky PDS client for posting bots: log in, post,
set the profile picture. About 250 lines of XRPC over `httpx`, no SDK.

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
```

## What it does with failures

| Response | `login` (createSession) | `post` (createRecord) |
|---|---|---|
| `429` | Retried, honouring `Retry-After` | Retried, honouring `Retry-After` |
| `5xx` | Retried | `AmbiguousWriteError`, not retried |
| Transport error (timeout, reset) | Raised as `httpx.TransportError` | `AmbiguousWriteError` |
| `2xx` with no readable `uri` | — | `AmbiguousWriteError` |
| `400 ExpiredToken`/`InvalidToken`, or `401` | — | One re-login, one retry |
| Other `4xx` | `BlueskyError` | `BlueskyError` |

**An ambiguous write is the caller's problem, deliberately.** The PDS may have
committed the record and then failed to say so. Retrying would risk a
duplicate on a public timeline, which can't be undone. Missing one post can be
recovered from. So the library never retries it, and the app has to record
that slot or row as attempted. `AmbiguousWriteError` subclasses
`BlueskyError`. If you catch it, catch it first.

`set_avatar` is state, not an event: setting the same picture twice is
harmless, so any failure is safe to retry on the next tick. It keeps the rest
of the profile record and uses `swapRecord`, so a hand edit made between the
read and the write is not overwritten.

## Not in scope

Async, rate limiting of AppView reads, scheduling, deduplication, templating,
and dry-run all live in the apps. That's where the bots differ.

## Using it from an app

Pin a tag's tarball. The fleet's Docker build images have no `git`, so a
git dependency would fail inside `docker compose build`:

```toml
dependencies = [
    "atproto-post @ https://github.com/danhon/atproto-post/archive/refs/tags/v0.1.0.tar.gz",
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
