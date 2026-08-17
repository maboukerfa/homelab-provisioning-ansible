# litellm

One OpenAI-compatible endpoint in front of every LLM provider, with virtual
keys and per-key spend tracking. Deployed with
`ansible-playbook playbooks/litellm.yml`.

| | |
|---|---|
| Host path | `/opt/litellm/` (compose file, root-owned) |
| Data | `/srv/litellm/postgres` (bind mount, uid 2000 / appuser) |
| Published on | `http://<host>:8006/`, UI at `http://<host>:8006/ui` |
| Secrets | `/opt/litellm/.env` — three of them, see `.env.example` |
| Backups | `pg_dump`, **not** `git_archive` — see [Backups](#backups) |

Two containers: the gateway, and a Postgres holding models, virtual keys and
spend logs. `STORE_MODEL_IN_DB` is on, so there is no `config.yaml` — models
are added through the UI and the database is the only source of truth.

## Setup

The playbook creates and chowns `/srv/litellm/postgres` itself, so the only
manual step is the three secrets — deliberately not in git. On the host:

```sh
sudo install -d -o root -g root -m 0755 /opt/litellm

printf 'LITELLM_MASTER_KEY=sk-%s\nLITELLM_SALT_KEY=%s\nPOSTGRES_PASSWORD=%s\n' \
    "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" \
    | sudo install -o root -g root -m 0640 /dev/stdin /opt/litellm/.env

sudo cat /opt/litellm/.env    # copy the master key into your password manager
```

Then from this repo: `ansible-playbook playbooks/litellm.yml`.

All three are required — compose refuses to start without any of them, rather
than coming up on a published placeholder. They do different jobs:

| | |
|---|---|
| `LITELLM_MASTER_KEY` | admin credential. Logs into the UI and mints virtual keys. `sk-` prefix required |
| `LITELLM_SALT_KEY` | encrypts provider API keys at rest in the database. **Never changes** |
| `POSTGRES_PASSWORD` | gateway → database. Never leaves the compose network |

**`LITELLM_SALT_KEY` is the one to be careful with.** Rotating it does not
re-encrypt anything: every provider key already in the database becomes
undecryptable, and every model stops working until you paste its key in again.
It is only useful alongside the database it encrypted, so back it up with the
dump, not on its own.

## First login

Open `http://<host>:8006/ui` and log in with username **`admin`** and the
master key as the password. That default is `UI_USERNAME` / `UI_PASSWORD`
falling back to the master key when unset, and neither is set here — one fewer
credential to lose, and the master key already grants everything the UI can do.

Then add a provider under **Models**, paste its API key, and hand out a virtual
key per client rather than sharing the master key. Provider keys entered here
are stored in Postgres encrypted with `LITELLM_SALT_KEY`; the master key stays
in your password manager and out of every client config.

Any OpenAI client works against it:

```sh
curl http://<host>:8006/v1/chat/completions \
    -H 'Authorization: Bearer sk-<a virtual key>' \
    -H 'Content-Type: application/json' \
    -d '{"model": "<name you gave it>", "messages": [{"role": "user", "content": "hi"}]}'
```

## Reaching it

Published on `8006` on every interface, so anything on the LAN can reach it.
That is fine as long as it is a LAN you trust: every route is gated by the
master key or a virtual key, and the database is not published at all.

It is still plain HTTP, which means keys cross the network in the clear. Once
there is a reverse proxy on this host, put it behind TLS and stop publishing
the port to the LAN:

```yaml
    ports:
      - '127.0.0.1:8006:4000'
```

An SSH tunnel gets you there in the meantime, and is worth using for the UI:

```sh
ssh -N -L 8006:localhost:8006 maboukerfa@<host>
```

**Do not put this on the public internet without more thought than a port
forward.** A gateway holding provider keys, reachable by anyone who guesses a
key, bills you for their traffic.

## Postgres runs as appuser, not as 999

The postgres image has its own `postgres` user at uid 999 and the upstream
quickstart just uses it. This stack pins `user: "2000:2000"` instead, so the
database is owned by the same identity as every other container here:

```sh
sudo ls -ld /srv/litellm /srv/litellm/postgres
# drwxr-x--- 2000 2000  /srv/litellm
# drwx------ 2000 2000  /srv/litellm/postgres
```

**Why not 999:** on a Debian host it is almost never free. System accounts are
allocated downwards from 999, so it usually already belongs to something — on
this host, `caddy`, with gid 999 belonging to `systemd-journal`:

```sh
getent passwd 999
```

Ownership is numeric, so that account really does own the files: a process
running as it could read the database and write to it. Nothing breaks, and it
is easy to miss, which is what makes it worth avoiding.

**Why it works.** `initdb` is the one part of Postgres that insists on finding
its own uid in `/etc/passwd` — it looks the uid up by name and exits if that
fails — and the entrypoint already handles that. When `getent passwd $(id -u)`
comes up empty it preloads `libnss_wrapper` and fabricates the entry for the
duration of `initdb`, then drops it. The image installs `libnss-wrapper`
specifically for this. Everything past initialisation is content with a bare
numeric uid, which is how these images run on OpenShift.

Pinning the uid also keeps the container out of root: left to itself the
entrypoint starts as root, chowns the data directory and drops privileges,
which needs `CAP_CHOWN`, `CAP_SETUID` and `CAP_SETGID` — the opposite of the
`cap_drop: ALL` in the compose file. Non-root from the start, it skips all of
it, and Ansible's ownership stands.

The playbook creates `/srv/litellm` explicitly for a smaller reason: Ansible's
`file` module is `mkdir -p` and stamps the owner and mode it was given onto
every parent it has to create, so an implicit parent would inherit the data
directory's `0700` by accident rather than by decision.

## Rotating the Postgres password

`POSTGRES_PASSWORD` is read **once**, when `initdb` creates the cluster.
Editing `.env` afterwards changes what the gateway sends and nothing about what
the database expects, so the stack comes back up with the gateway unable to log
in. Change both:

```sh
sudo docker exec -it litellm-db psql -U litellm -c "ALTER USER litellm PASSWORD 'new-password';"
sudo $EDITOR /opt/litellm/.env
ansible-playbook playbooks/litellm.yml
```

The master key rotates freely — edit `.env` and redeploy. Virtual keys already
issued keep working; they are rows in the database, not derivations of it.

## Backups

**Do not point `git_archive` at `/srv/litellm/postgres`.** That role commits a
directory as it finds it, which for a live database means a torn snapshot: page
files copied mid-write, in a tree git stores badly and cannot meaningfully diff.
The quiet-period check does not help — Postgres writes whenever it feels like
it, not when you are looking.

The right tool is a dump, which is consistent by definition:

```sh
sudo docker exec litellm-db pg_dump -U litellm -Fc litellm > litellm-$(date +%F).dump
```

Restore into an empty stack with `pg_restore -U litellm -d litellm`. Keep
`LITELLM_SALT_KEY` with the dump — without it the provider keys inside are
ciphertext nobody can read.

## Verifying

```sh
sudo docker compose -f /opt/litellm/compose.yaml ps      # both (healthy)
sudo docker compose -f /opt/litellm/compose.yaml logs -f litellm
curl -fsS http://<host>:8006/health/readiness            # {"status":"healthy","db":"connected"}
ls -ld /srv/litellm/postgres                             # 0700, owned by 999
```

The first start takes a minute: the gateway runs the whole prisma migration
against an empty database before it opens the port, which is what the 90s
`start_period` on its healthcheck is for.

`/health/readiness` is the endpoint the healthcheck uses, and it answers `503`
when the database is unreachable — so `(healthy)` here means the gateway can
actually reach Postgres, not merely that the process is running. `/health`
(with the master key) is the different, more expensive one: it calls every
configured model to see which are actually answering.
