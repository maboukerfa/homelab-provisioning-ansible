# stacks/

One directory per containerised app. This is the versioned source of truth for
what runs on the homelab; the host holds a deployed copy, not the original.

```
stacks/
  <name>/
    compose.yaml     # committed
    .env.example     # committed -- documents the required keys, holds no values
    README.md        # optional: why this stack is configured the way it is
```

`compose.yaml` is the name the Compose spec settled on. Docker still picks up
`docker-compose.yml`, so nothing existing needs renaming to move in here.

## How a stack maps onto the host

```
stacks/foo/compose.yaml   ->  /opt/foo/compose.yaml    root-owned, appuser-readable
                              /opt/foo/.env            root-owned, 0640, NOT from git
                              /srv/foo/                appuser-owned, the data
```

Two rules make the rest of the homelab simpler:

- **`/opt` is reproducible, `/srv` is precious.** Everything under `/opt` can be
  recreated from this repo. Everything under `/srv` cannot. Whatever you point
  at `/srv` later has the whole backup problem covered, and the boundary is a
  directory rather than a judgement call you re-make for every new service.
- **appuser runs containers, it does not own their definitions.** Compose files
  stay root-owned. A container escape that lands as appuser cannot then rewrite
  the compose file that describes what it is allowed to do.

## Secrets

`.env` files are gitignored and never leave the host. Commit `.env.example` with
the keys and no values, and have the compose file fail loudly on a missing one
rather than starting up half-configured:

```yaml
environment:
  LITELLM_MASTER_KEY: ${LITELLM_MASTER_KEY:?set LITELLM_MASTER_KEY in .env, sk- prefixed}
```

## Running as appuser

The bootstrap pins appuser to `2000:2000`. Images that support it take
`PUID`/`PGID`; images that do not — a static binary with no entrypoint juggling —
take a `user:` line:

```yaml
user: "2000:2000"
```

Whichever you use, `/srv/<name>/` has to be owned by the same ids or the
container comes up unable to write, which looks like a working service right up
until the first save.

`immich` is the one stack that had to be migrated onto appuser rather than
starting there — it was adopted from a root install, which is a recursive chown
and a rehearsal, not an edit. See `stacks/immich/README.md`. Its valkey is
still the image's own uid 999, because that image drops privileges itself and
writes nothing outside the container.
