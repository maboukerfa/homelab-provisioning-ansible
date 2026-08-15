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
  recreated from this repo, so it needs no backup. Everything under `/srv` cannot,
  so restic backs it up. The backup boundary is a directory, not a judgement call
  you have to make again for every new service.
- **appuser runs containers, it does not own their definitions.** Compose files
  stay root-owned. A container escape that lands as appuser cannot then rewrite
  the compose file that describes what it is allowed to do.

## Secrets

`.env` files are gitignored and never leave the host. Commit `.env.example` with
the keys and no values, and have the compose file fail loudly on a missing one
rather than starting up half-configured:

```yaml
environment:
  SB_USER: ${SB_USER:?set SB_USER in .env as user:password}
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
