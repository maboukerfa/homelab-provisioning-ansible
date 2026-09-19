# immich

A self-hosted photo and video library: originals on disk, everything about them
in Postgres, machine learning for search and faces. Deployed with
`ansible-playbook playbooks/immich.yml`.

| | |
|---|---|
| Host path | `/opt/immich/` (compose file, root-owned) |
| Library | `/srv/immich-images/images` — 34G on its own 246G LVM volume |
| Database | `/srv/immich/postgres` — on the root filesystem, uid 999 |
| Model cache | docker volume `immich_model-cache` — ~2GB, regenerable |
| Published on | `2283` on every interface — the mobile apps upload to the LAN address |
| Served at | `https://immich.bat-kochab.ts.net` via `tailscale serve` |
| Runs as | **root**, unlike every other stack here — see [Running as root](#running-as-root) |
| Secrets | `/opt/immich/.env` — one key, see `.env.example` |
| Version | **v3.2.2**, pinned — see [Upgrading](#upgrading) |
| Backups | restic timer + Immich's own nightly dump — see [Backups](#backups) |

Four containers: the server, a machine-learning container that does CLIP
embeddings and face detection, a Postgres carrying VectorChord and pgvecto.rs
for the vector queries those produce, and a valkey holding the job queue.

## This is an adoption

Immich was installed here in July 2026 from the upstream compose file at
`/opt/immich-app/docker-compose.yml` and had been running untouched since. This
directory is that install, moved into the repo — not a rewrite. The resolved
configuration differs in exactly three places, and `docker compose config`
before and after was diffed to prove it:

- **the image tags are pinned** (below — this is the whole point);
- **`env_file: .env` is gone.** Upstream hands every variable in `.env` to both
  the server and the machine-learning container, which is how the model runner
  ends up holding the database password. What Immich actually reads is now
  named explicitly in `environment:`;
- **the non-secrets left `.env`.** `UPLOAD_LOCATION`, `DB_DATA_LOCATION`,
  `IMMICH_VERSION`, `DB_USERNAME` and `DB_DATABASE_NAME` describe the
  deployment rather than protecting it, so they are in `compose.yaml` where a
  diff shows them changing. Only `DB_PASSWORD` is still a secret.

`database` and `redis` resolve byte-identically either side of the move. They
were recreated on the first deploy anyway, along with the other two: the
compose file moved from `/opt/immich-app` to `/opt/immich`, and Compose stamps
the project path onto every container as a label, which is part of what it
compares before deciding a container is still current. A one-off, and the
reason `docker compose ls` now names the right file.

## Upgrading

The reason this stack is in the repo at all.

Upstream's file reads its tag from `${IMMICH_VERSION}` in `.env`, and this host
had `v3` — a rolling tag. It pointed at **v3.0.3** when it was pulled in July
and at **v3.2.2** by September. Nothing here had decided to upgrade; the next
`docker compose pull` for any reason would have done it, and Immich runs its
schema migrations on start and does not run them backwards.

So the adoption pinned `v3.0.3`, the bits already running, and the move to
`v3.2.2` was the next commit — taken deliberately, after reading the notes,
rather than collected as a side effect. That is the whole difference.

```sh
# 1. Read what changed. Immich calls out required migrations and breaking
#    changes in the release notes, and skipping majors is not supported.
#    https://github.com/immich-app/immich/releases
# 2. Take a dump. Immich's own nightly one can be 24h old, and rolling back an
#    upgrade is a restore, not a tag revert.
ssh homelab 'sudo sh -c "docker exec immich_postgres pg_dumpall --clean \
  --if-exists --username=postgres | gzip > \
  /srv/immich-images/images/backups/pre-upgrade-$(date +%F).sql.gz"'
# 3. Bump BOTH immich tags in compose.yaml. They share an API contract -- a
#    mismatched pair shows up as search and face detection quietly failing.
# 4. Deploy, and watch it migrate.
make stack NAME=immich
ssh homelab 'docker logs -f immich_server'
```

Name that dump anything except `immich-db-backup-*.sql.gz`: Immich prunes its
own backups by that pattern and would eventually delete yours.

The Postgres image is pinned by digest and its tag names three things that are
on-disk format — the major, `vectorchord0.4.3` and `pgvectors0.2.0`. Changing
any of them is a migration in its own right, and Immich's release notes say
when one is required. Do not bump it because it looks stale.

**Rolling back an upgrade is a restore, not a tag revert.** The old image will
refuse the migrated schema. That means the nightly dump is the rollback path,
which is worth knowing *before* running the upgrade rather than after.

## The two ways this install could be destroyed

Both are quiet, both are a missing directory, and `playbooks/immich.yml`
refuses to deploy on either.

**The library volume is not the root disk.** `/srv/immich-images` is a separate
LVM volume. If it is not mounted, the bind source is an empty directory on `/`
— Immich starts, reports healthy, shows an empty library and begins refilling
200G of space that does not exist. The playbook asserts the mount point.

**An empty `DB_DATA_LOCATION` is a new install.** Postgres initdb's a fresh
cluster when its data directory is empty, so a missing or unmounted
`/srv/immich/postgres` gives you a factory-fresh Immich — no users, no albums,
no faces — sitting on top of 34G of photos it has no rows for. The playbook
asserts `PG_VERSION` is there.

Neither check costs anything and neither would have been caught downstream: in
both cases every container comes up healthy and the API answers.

## Running as root

Every other stack in this repo runs as appuser (2000:2000). This one runs as
root, which is Immich's supported configuration and what this host has been
doing for months, and it stays that way on purpose: changing it is a migration,
not a deploy.

What it would take, when you want it — as its own commit, with the stack down:

```sh
# the library, 34G, and the ~2GB model cache
chown -R 2000:2000 /srv/immich-images/images
# the cluster, currently uid 999 -- which on this host is `caddy`, so ls lies
# about who owns it
chown -R 2000:2000 /srv/immich/postgres
```

plus `user: "2000:2000"` on the server, the machine-learning container and the
database, and `cap_drop: [ALL]` becoming meaningful at the same time. It is not
meaningful now: the server writes into a bind mount whose directories belong to
three different uids, and it is `DAC_OVERRIDE` under a root uid that lets it.
Dropping capabilities while staying root would be decoration.

Worth doing. A container with the whole photo library bind-mounted is the one
on this host where root matters most.

## Data layout

```
/srv/immich-images/          own LVM volume, 246G
  images/                    UPLOAD_LOCATION, bind-mounted to /data
    library/          26G    originals -- the irreplaceable part
    encoded-video/    5.6G   transcodes, regenerated from originals
    thumbs/           2.1G   thumbnails, likewise
    upload/            26M   in flight
    profile/           32K   avatars
    backups/          372M   Immich's own nightly pg_dump, 02:00

/srv/immich/
  postgres/          351M    the live cluster
  library/ thumbs/ upload/ encoded-video/ profile/ backups/   ~6G, STALE
```

That second group is the original upload location, from before the dedicated
volume existed. Nothing mounts it — the July 26 timestamps are the move. It is
6G of rot with the same directory names as the live library, which makes it the
thing somebody restores from by mistake one day. Check it against the live
library and delete it.

## Backups

Two jobs, neither managed by this repo, both worth knowing about:

- **Immich dumps its own database** nightly at 02:00 into
  `/data/backups` — that is `/srv/immich-images/images/backups`, on the photo
  volume. Configured in the admin UI, not here.
- **restic** runs at 03:00 from `/opt/backup/immich-backup.sh`
  (`immich-backup.timer`), an hour later so each snapshot pairs the library
  with a fresh dump. It saves `library/`, `profile/`, `upload/` and `backups/`,
  skips `thumbs/` and `encoded-video/` because Immich rebuilds those, and
  refuses to run silently on a stale dump. It never copies the live cluster
  directory, which would not restore.

**One loose end after this adoption.** That script also backs up
`$IMMICH_APP_DIR`, defaulting to `/opt/immich-app`. The compose file is now in
git and the `.env` is deliberately left behind there, so nothing is broken —
but to finish the move:

```sh
echo 'IMMICH_APP_DIR=/opt/immich' >> /etc/backup/restic.env   # holds restic's credentials
systemctl start immich-backup.service                          # confirm it is green
rm -rf /opt/immich-app
```

The playbook prints this reminder on every run until `/opt/immich-app/.env` is
gone. It does not edit `/etc/backup/restic.env` itself: that file is a secret
this repo did not write.

## Reaching it

`tailscale serve service=svc:immich` proxies
`https://immich.bat-kochab.ts.net` to `localhost:2283`, from
`tailscale-immich-serve.service` on the host — not from this repo, and not
Caddy, which fronts the other stacks here.

Port 2283 is published on every interface rather than loopback. The tailnet
proxy would be happy either way; the mobile apps uploading straight to the LAN
address would not, and that is the kind of thing that breaks silently and is
noticed a week later.

## The database password

It is `postgres`. It never leaves the compose network — the database publishes
no port and only the server authenticates with it — so this is untidy rather
than urgent, but it is untidy in a file that now documents itself as a secret.

Rotating it is two steps in this order, because the compose variable is read at
initdb time only and has no effect on a cluster that already exists:

```sh
docker exec -it immich_postgres psql -U postgres -c "ALTER USER postgres PASSWORD '<new>';"
# then put <new> in /opt/immich/.env and redeploy
```
