# dawarich

Self-hosted location history — a map of everywhere you have been, fed by a
phone app and by Google Takeout imports. Deployed with
`ansible-playbook playbooks/dawarich.yml`.

| | |
|---|---|
| Host path | `/opt/dawarich/` (compose file, root-owned) |
| Data | `/srv/dawarich/{postgres,public,storage,watched,redis}` (uid 2000 / appuser) |
| Published on | `http://<host>:8005/`, `https://dawarich.bat-kochab.ts.net` on the tailnet |
| Secrets | `/opt/dawarich/.env` — two, both generated, see `.env.example` |
| Backups | `pg_dump`, **not** git — see [Backups](#backups) |

Four containers: the Rails app, a Sidekiq worker that does the imports and the
reverse geocoding, a PostGIS database, and a redis holding the job queue.

## The state this was adopted from

This stack existed on the host before it existed in this repo — the upstream
compose file, dropped in `/opt/dawarich/` by hand, running unchanged. Two
things about it are worth writing down, because both are what the upstream file
does by default and neither announces itself:

- **`SECRET_KEY_BASE` was the literal string `CHANGE_ME`.** There was no `.env`
  on the host, so every `${VAR:-default}` in that file took its default. Rails
  signs the session cookie with that key, so anyone who has read the upstream
  compose file could mint a valid session for any account — and this instance
  answers on the public internet through a Cloudflare tunnel. Fixed by
  requiring the variable, with no default, in `compose.yaml`.
- **`POSTGRES_PASSWORD` was `password`.** Lower stakes — the database publishes
  no port and is reachable only from the compose network — but a real
  credential all the same.

Rotating `SECRET_KEY_BASE` signs everyone out and nothing else, as long as no
account has 2FA enabled. It is also the seed for the ActiveRecord encryption
keys that protect OTP secrets, so once someone does turn 2FA on, set
`OTP_ENCRYPTION_PRIMARY_KEY`, `OTP_ENCRYPTION_DETERMINISTIC_KEY` and
`OTP_ENCRYPTION_KEY_DERIVATION_SALT` explicitly, and rotate them separately.

## Migrating from the upstream layout

**One-off, and only on a host that already ran the upstream compose file.** A
fresh host skips to [Setup](#setup). The data moves out of five docker named
volumes into `/srv/dawarich/`, and the containers stop being root. Budget a few
minutes of downtime; nothing is deleted, so the old volumes stay as the
rollback path.

```sh
# 1. Rotate the database password while the old stack is still up -- the
#    cluster already exists, so the .env below cannot do it on its own.
POSTGRES_PASSWORD=$(openssl rand -hex 32)
sudo docker exec dawarich_db psql -U postgres \
    -c "ALTER USER postgres PASSWORD '$POSTGRES_PASSWORD';"

# 2. Keep the old compose file. The playbook removes it, and it is what a
#    rollback needs.
sudo cp /opt/dawarich/docker-compose.yml ~/dawarich-compose.pre-ansible.yml

# 3. Stop the stack.
sudo docker compose -f /opt/dawarich/docker-compose.yml down

# 4. Copy each volume to where the new compose file expects it. Directories
#    first, then `src/. dst/` rather than `src dst` -- the second form nests a
#    stray _data/ inside any directory the playbook has already created.
#    cp -a, so timestamps survive the trip; the volumes are left intact.
V=/var/lib/docker/volumes
sudo install -d -o appuser -g appuser -m 0750 \
    /srv/dawarich /srv/dawarich/public /srv/dawarich/storage \
    /srv/dawarich/watched /srv/dawarich/redis
sudo install -d -o appuser -g appuser -m 0700 /srv/dawarich/postgres

sudo cp -a $V/dawarich_dawarich_db_data/_data/. /srv/dawarich/postgres/
sudo cp -a $V/dawarich_dawarich_public/_data/.  /srv/dawarich/public/
sudo cp -a $V/dawarich_dawarich_storage/_data/. /srv/dawarich/storage/
sudo cp -a $V/dawarich_dawarich_watched/_data/. /srv/dawarich/watched/
sudo cp -a $V/dawarich_dawarich_shared/_data/.  /srv/dawarich/redis/

# 5. Hand it all to appuser. It arrives owned by whatever uid the root
#    containers used -- 70 for postgres, 999 for redis -- and a container
#    pinned to 2000 would come up unable to write a byte.
sudo chown -R appuser:appuser /srv/dawarich
sudo chmod 0700 /srv/dawarich/postgres

# 6. Both secrets. POSTGRES_PASSWORD is the one set in step 1.
printf 'SECRET_KEY_BASE=%s\nPOSTGRES_PASSWORD=%s\n' \
    "$(openssl rand -hex 64)" "$POSTGRES_PASSWORD" \
    | sudo install -o root -g root -m 0640 /dev/stdin /opt/dawarich/.env
```

Then from this repo:

```sh
ansible-playbook playbooks/dawarich.yml
```

Everyone signs in again on the next visit — that is the new `SECRET_KEY_BASE`,
and it is the point.

Once the map has drawn itself and the point count looks right, reclaim the
~155MB the old copy holds:

```sh
sudo docker volume rm dawarich_dawarich_db_data dawarich_dawarich_public \
    dawarich_dawarich_shared dawarich_dawarich_storage dawarich_dawarich_watched
```

**To roll back instead**, restore the file from step 2 and bring it up with
`docker compose -f`. The volumes are untouched, so it comes back exactly as it
was — with the old password, which step 1 changed inside the database. Set it
back with the same `ALTER USER`.

## Setup

On a host with no Dawarich on it, the only manual step is the two secrets, and
both are generated:

```sh
sudo install -d -o root -g root -m 0755 /opt/dawarich

printf 'SECRET_KEY_BASE=%s\nPOSTGRES_PASSWORD=%s\n' \
    "$(openssl rand -hex 64)" "$(openssl rand -hex 32)" \
    | sudo install -o root -g root -m 0640 /dev/stdin /opt/dawarich/.env
```

Addresses on the map are a separate, deliberate decision — see
[Reverse geocoding](#reverse-geocoding).

Then `ansible-playbook playbooks/dawarich.yml`. The playbook creates and chowns
every directory under `/srv/dawarich` itself; the first start runs the whole
schema migration against an empty database, so give it a minute.

Registration is open on a fresh instance and there is no admin approval step —
create your account, then close the door. Dawarich has no setting for this;
`APPLICATION_HOSTS` and the tunnel are the door.

## Reaching it

Three ways in, all to the same container:

| | |
|---|---|
| `https://dawarich.bat-kochab.ts.net` | `tailscale serve`, tailnet only — the normal way |
| `https://dawarich.boukerfa.eu` | Cloudflare tunnel, **public** |
| `http://<host>:8005` | direct, LAN, plain HTTP |

The public route is the one to think about: it is a login page on the open
internet in front of every place you have been. It is not configured from this
repo — the tunnel is a `cloudflared` service on the host taking its routes from
the Cloudflare dashboard — so closing it is done there. Dropping
`dawarich.boukerfa.eu` from `APPLICATION_HOSTS` in `compose.yaml` is the
belt-and-braces half: Rails then rejects the request on the Host header even if
the tunnel is still pointed at it.

Port 8005 is published on every interface rather than on loopback, which the
two proxies would be equally happy with. That is deliberate and worth
revisiting: a phone posting locations straight to the LAN address would stop
working if it moved, and would do it silently.

## Everything runs as appuser

Four containers, two mechanisms, because the images differ:

| | how | why |
|---|---|---|
| app, sidekiq | `PUID`/`PGID` | the entrypoint chowns `/var/app/{db,log}`, then `gosu`s down. A `user:` line starts unprivileged and never gets to |
| db, redis | `user: "2000:2000"` | no entrypoint juggling to accommodate |

Which is why the app and the worker keep four capabilities — `CHOWN`,
`DAC_OVERRIDE`, `SETGID`, `SETUID` — where every other container in this repo
drops all of them. That is the cost of an entrypoint that starts as root; the
process that actually serves traffic has none.

The database is `postgis/postgis:17-3.5-alpine` rather than plain Postgres and
that is not interchangeable — the schema has geometry columns and a spatial
index. It runs as 2000 for the reasons written up in
[`../litellm/README.md`](../litellm/README.md#postgres-runs-as-appuser-not-as-999):
the image's own uid 999 already belongs to `caddy` on this host, and `initdb`'s
need for a passwd entry is handled by the `libnss_wrapper` the image ships.

## Reverse geocoding

What turns `43.6108, 3.8767` into a street and a city — the addresses on the
points, the place names in Visits, and the search that finds them.

**Not configured here.** `STORE_GEODATA: true` caches whatever a geocoder
returns, but no geocoder is set, so nothing is fetched and nothing is cached:
points keep their coordinates and never get an address. That is upstream's
default too, and leaving it is a decision rather than an oversight — every
option below but one hands a location history to somebody else.

### Turning it on

Dawarich has no per-vendor setting; it speaks Photon, and several services
answer as one. Three variables, on **both** `dawarich_app` and
`dawarich_sidekiq` in `compose.yaml` — the worker is what actually geocodes:

```yaml
PHOTON_API_HOST: app.chibigeo.com/v1/photon
PHOTON_API_USE_HTTPS: true
PHOTON_API_KEY: ${PHOTON_API_KEY:?set PHOTON_API_KEY in .env}
```

`PHOTON_API_HOST` is documented as a bare hostname and is a bare hostname for
every other Photon; chibigeo needs the path glued on. `PHOTON_API_KEY` only
applies to a provider that issues one — drop that line, and the `.env` entry it
reads, for a self-hosted or public instance. Redeploy after.

### Choosing a provider

The variables are the easy part; who sees the coordinates is the decision.

| | privacy | cost | limits |
|---|---|---|---|
| Self-hosted Photon | nothing leaves the host | disk: a planet index is large, country extracts are not | none |
| chibigeo | coordinates leave the host, to the Dawarich team | paid, flat rate | per plan |
| Public Photon | coordinates leave the host, to a stranger | free | per instance, [community list](https://github.com/Freika/dawarich/discussions/693) |
| Geoapify / Nominatim | coordinates leave the host, to a vendor | free tier | tight; painful on a backfill |

Self-hosted Photon (`rtuszik/photon-docker` is the batteries-included image) is
the only row where a location history is not handed to somebody. It is a
service to run and an index to keep on disk; chibigeo, the hosted
Photon-compatible service run by the Dawarich team, is the row that costs money
instead. That trade is the whole of it — swapping later is these three lines
and a redeploy, so it is not a decision to agonise over.

### After enabling, or changing provider

Existing points keep the addresses they already have and new ones get the new
provider's. To reconcile, use **Settings → Background Jobs**:

- **Continue** — geocodes only points that have never been processed. What to
  press after enabling geocoding on a history that predates it.
- **Restart** — re-geocodes every point. A full history against a rate-limited
  provider is a long queue; watch it on the Sidekiq page.

### When it stops working

An expired plan or a revoked key does not stop the stack — `${VAR:?}` only
fires when the key is missing from `.env`, and an invalid one is still a
present one. The symptom is quieter: new points arrive with no address, and
Sidekiq collects failed reverse-geocoding jobs. The Sidekiq page's retry and
dead queues are where it shows up first.

```sh
sudo docker logs dawarich_sidekiq 2>&1 | grep -i 'photon\|geocod'
```

To turn geocoding back off, drop the `PHOTON_*` lines from both containers and
redeploy. Cached geodata survives; nothing new is looked up.

## Rotating the Postgres password

`POSTGRES_PASSWORD` is read **once**, when `initdb` creates the cluster.
Editing `.env` afterwards changes what the app sends and nothing about what the
database expects. Change both:

```sh
sudo docker exec dawarich_db psql -U postgres -c "ALTER USER postgres PASSWORD 'new-password';"
sudo $EDITOR /opt/dawarich/.env
ansible-playbook playbooks/dawarich.yml
```

`SECRET_KEY_BASE` rotates freely — edit `.env` and redeploy. Everyone signs in
again; nothing stored depends on it while no account has 2FA.

## Backups

**Do not put `/srv/dawarich` under git.** Committing a directory as you find
it means, for a live database, a torn snapshot — in a tree git stores badly and
cannot meaningfully diff. `/srv/dawarich/postgres` is
also the bulk of the stack.

The database is a dump:

```sh
sudo docker exec dawarich_db pg_dump -U postgres -Fc dawarich_development \
    > dawarich-$(date +%F).dump
```

Restore into an empty stack with `pg_restore -U postgres -d
dawarich_development`. It carries the whole history — points, imports, trips,
cached geocoding.

`/srv/dawarich/storage` holds uploaded import files and generated exports and
is worth keeping with the dump; `public`, `watched` and `redis` are all
reproducible and not worth backing up. Nothing under `/srv/dawarich` needs a
key to read later, so unlike LiteLLM there is no secret to store alongside it.

## Verifying

```sh
sudo docker compose -f /opt/dawarich/compose.yaml ps      # four, all (healthy)
sudo docker compose -f /opt/dawarich/compose.yaml logs -f dawarich_app
curl -fsS http://<host>:8005/api/v1/health                # {"status":"ok"}
sudo ls -ld /srv/dawarich/*                               # all appuser, postgres 0700
sudo docker exec dawarich_app id                          # uid=2000
```

`(healthy)` on `dawarich_app` means the Rails app answered its own health
endpoint. `dawarich_sidekiq` only checks that the process is alive — a worker
that is up but not draining its queue still reports healthy, so the queue depth
on the web UI's Sidekiq page is the thing to look at after a large import.

## Deviations from the upstream compose file

Everything here started as `https://dawarich.app/docs/self-hosting`, so the
differences are the interesting part:

| | |
|---|---|
| Image pinned to `1.14.0` | upstream uses `latest`, which has already moved past what this host runs |
| Data in `/srv/dawarich/` | upstream uses five named volumes; `/srv` is this repo's backup boundary |
| Non-root | upstream runs all four as root; `PUID`/`PGID` are commented out in its file |
| Secrets required | upstream defaults them to `CHANGE_ME` and `password` |
| No `dawarich_db_data` in the app | upstream mounts the Postgres data directory into the web app, read-write. Nothing reads it |
| No `/var/shared` in the database | upstream mounts redis's volume there. It holds `dump.rdb` and nothing else |
| No `logging:` block | `/etc/docker/daemon.json` already caps json-file at 10m × 3, host-wide, from `roles/docker` |
| No explicit network | four services, all on it, nothing else joining — the default network is the same thing with a shorter name |
| No `stdin_open`/`tty` | nothing attaches to these containers |
| Literal values, not `${VAR:-default}` | one source of truth per setting, greppable. `${VAR:?}` is kept for both secrets, where the failure is the feature |
